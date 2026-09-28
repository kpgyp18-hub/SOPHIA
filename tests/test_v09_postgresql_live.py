"""Live PostgreSQL verification for the v0.9 long-horizon runtime.

The rest of the v0.9 suite exercises the record layer through SQLite plus a
PostgreSQL migration mock. Neither can observe server-enforced behaviour: the
SQLite adapter never issues ``PRAGMA foreign_keys=ON``, so every declared
foreign key and cascade is inert there. This module drives the real Phase C-F
components against a running PostgreSQL instance instead.

It is opt-in. Set ``SOPHIA_PG_LIVE_TEST=1`` to run it, because it writes to and
deletes from the configured database. Every row it creates is namespaced under a
verification goal and removed in teardown.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from sophia.database import DatabaseManager
from sophia.goals import (
    ApprovalQueue, ApprovalStatus, ArtifactState, ArtifactTracker, AutonomyBudget,
    AutonomyBudgetTracker, FailureBudgetManager, GoalManager, GoalStatus, GoalTaskStatus,
    HeartbeatManager, PlanManager, ProgressTracker, ResumeManager, Scheduler,
    TaskLeaseManager, TaskQueue, TriggerType, Watchdog,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SOPHIA_PG_LIVE_TEST") != "1",
    reason="live PostgreSQL test is opt-in; set SOPHIA_PG_LIVE_TEST=1",
)

CONFIG = {
    "active": "postgresql",
    "fallback": "sqlite",
    "backends": {
        "postgresql": {"host": "localhost", "port": 5432, "database": "sophia", "user": "sophia"},
        "sqlite": {"path": "workspace/sophia/pg_live_fallback.db"},
    },
}


@pytest.fixture(scope="module")
def database() -> DatabaseManager:
    manager = DatabaseManager(CONFIG)
    backend = manager.selected()
    if backend.name != "postgresql":
        pytest.skip(f"PostgreSQL unavailable; manager selected {backend.name}")
    health = backend.health()
    if not health.ok:
        pytest.skip(f"PostgreSQL unreachable: {health.detail}")
    return manager


@pytest.fixture
def runtime(database):
    """Yield a live goal plus managers, then remove every row it produced."""
    goals, plans = GoalManager(database), PlanManager(database)
    goal = goals.create("PG live verification", success_criteria=["record layer verified"],
                        risk_ceiling="R2", autonomy_budget=AutonomyBudget(), activate=True)
    try:
        yield database, goals, plans, goal
    finally:
        backend = database.selected()
        with backend._connect() as db, db.cursor() as cursor:
            # dependency edges first: dependency_task_id has no ON DELETE CASCADE
            cursor.execute("DELETE FROM runtime.task_dependencies WHERE task_id IN "
                           "(SELECT task_id FROM runtime.long_tasks WHERE goal_id = %s)",
                           (goal.goal_id,))
            cursor.execute("DELETE FROM runtime.goals WHERE goal_id = %s", (goal.goal_id,))
            db.commit()


def test_goal_and_task_dag_persist_on_live_server(runtime):
    database, goals, plans, goal = runtime
    assert goals.get(goal.goal_id).title == "PG live verification"

    first = plans.create_task(goal.goal_id, "first step")
    second = plans.create_task(goal.goal_id, "second step", dependencies=[first.task_id])
    assert tuple(plans.get_task(second.task_id).dependencies) == (first.task_id,)
    assert len(plans.list_tasks(goal.goal_id)) == 2

    TaskQueue(plans).ready(goal.goal_id)
    assert plans.get_task(first.task_id).status is GoalTaskStatus.READY
    assert plans.get_task(second.task_id).status is not GoalTaskStatus.READY


def test_resume_packet_reloads_from_server_after_manager_restart(runtime):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "checkpoint me")
    TaskQueue(plans).ready(goal.goal_id)
    plans.transition(task.task_id, GoalTaskStatus.RUNNING)

    points = ResumeManager(database, plans, goals).pause_goal(
        goal.goal_id, checkpoint_ids={task.task_id: "cp-live-1"})
    assert goals.get(goal.goal_id).status is GoalStatus.PAUSED

    restored = ResumeManager(database, PlanManager(database), GoalManager(database))
    assert restored.packet(goal.goal_id)["latest_checkpoint"]["checkpoint_id"] == "cp-live-1"
    restored.resume(points[0].resume_id)
    assert GoalManager(database).get(goal.goal_id).status is GoalStatus.ACTIVE

    snapshot = ProgressTracker(GoalManager(database), PlanManager(database)).snapshot(goal.goal_id)
    assert snapshot["goal_id"] == goal.goal_id and snapshot["task_total"] == 1


def test_scheduler_due_and_gates_resolve_against_server_rows(runtime):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "scheduled step")
    TaskQueue(plans).ready(goal.goal_id)
    scheduler = Scheduler(database, plans)

    overdue = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    schedule = scheduler.add(task.task_id, trigger_type=TriggerType.TIME, next_run=overdue)
    assert Scheduler(database, PlanManager(database)).get(schedule.schedule_id).task_id == task.task_id
    assert any(item.task_id == task.task_id for item in scheduler.due(goal_id=goal.goal_id))

    scheduler.pause(schedule.schedule_id)
    assert not any(item.task_id == task.task_id for item in scheduler.due(goal_id=goal.goal_id))

    gated = plans.create_task(goal.goal_id, "needs approval", risk="R2", approval_requirement=True)
    with pytest.raises(PermissionError):
        scheduler.add(gated.task_id)


def test_approval_is_user_only_on_live_data(runtime):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "approval step")
    approvals = ApprovalQueue(database, plans)
    request = approvals.request(task.task_id, action="live pg write", reason="verification",
                                expected_effect="none", rollback_plan="delete verification goal")

    assert approvals.get(request.approval_id).status is ApprovalStatus.PENDING
    assert any(item.approval_id == request.approval_id
               for item in approvals.list(goal_id=goal.goal_id, status=ApprovalStatus.PENDING))

    with pytest.raises(PermissionError):
        approvals.approve(request.approval_id, actor="aide")

    assert approvals.approve(request.approval_id, actor="user").status is ApprovalStatus.APPROVED
    assert ApprovalQueue(database, PlanManager(database)).granted(task.task_id) is True


def test_lease_ownership_and_heartbeat_upsert_are_server_visible(runtime):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "leased step")
    leases = TaskLeaseManager(database, plans)
    lease = leases.acquire(task.task_id, "blanc", ttl_seconds=120)
    assert leases.active(task.task_id) is not None

    with pytest.raises(RuntimeError):
        TaskLeaseManager(database, PlanManager(database)).acquire(task.task_id, "aide")

    leases.release(lease.lease_id, owner="blanc")
    assert leases.active(task.task_id) is None

    heartbeats = HeartbeatManager(database, plans)
    heartbeats.beat(task.task_id, progress_marker="50%", current_step="live pg check")
    heartbeats.beat(task.task_id, progress_marker="75%", current_step="live pg check")
    assert HeartbeatManager(database, PlanManager(database)).latest(task.task_id)["progress_marker"] == "75%"
    assert len(database.list_goal_runtime_records("heartbeat", task_id=task.task_id)) == 1


def test_watchdog_pauses_stale_task_using_server_state(runtime):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "stalled step")
    TaskQueue(plans).ready(goal.goal_id)
    plans.transition(task.task_id, GoalTaskStatus.RUNNING)
    HeartbeatManager(database, plans).beat(task.task_id, progress_marker="50%",
                                           current_step="live pg check")

    def future():
        return datetime.now(timezone.utc) + timedelta(hours=2)

    watchdog = Watchdog(plans, HeartbeatManager(database, plans, clock=future), clock=future)
    assert task.task_id in watchdog.inspect(goal.goal_id, timeout_seconds=60)
    assert PlanManager(database).get_task(task.task_id).status is GoalTaskStatus.PAUSED


def test_budgets_accumulate_and_enforce_ceilings_on_live_data(runtime):
    database, goals, plans, goal = runtime
    budget = AutonomyBudgetTracker(database, goals)
    budget.consume(goal.goal_id, tool_calls=1)
    budget.consume(goal.goal_id, tool_calls=2)
    assert AutonomyBudgetTracker(database, GoalManager(database)).usage(goal.goal_id)["tool_calls"] == 3
    with pytest.raises(RuntimeError):
        budget.consume(goal.goal_id, tool_calls=10_000_000)

    failures = FailureBudgetManager(database, goals)
    failures.record(goal.goal_id, "provider_failure", limits={"provider_failure": 2})
    state = failures.record(goal.goal_id, "provider_failure", limits={"provider_failure": 2})
    assert state["counts"]["provider_failure"] == 2 and not state["degraded"]

    degraded = failures.record(goal.goal_id, "provider_failure", limits={"provider_failure": 2})
    assert degraded["degraded"]
    assert GoalManager(database).get(goal.goal_id).status is GoalStatus.PAUSED


def test_artifact_hash_persists_and_path_escape_stays_blocked(runtime, tmp_path):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "artifact step")
    target = tmp_path / "artifact.txt"
    target.write_text("live pg verification artifact\n", encoding="utf-8")

    artifacts = ArtifactTracker(database, tmp_path)
    record = artifacts.register(goal.goal_id, task.task_id, target.name,
                                artifact_type="text", version="1", state=ArtifactState.DRAFT)
    stored = database.get_goal_runtime_record("artifact", record["artifact_id"])
    assert stored["hash"] == record["hash"]

    with pytest.raises((PermissionError, FileNotFoundError)):
        artifacts.register(goal.goal_id, task.task_id, "../outside.txt",
                           artifact_type="text", version="1")


def test_every_runtime_record_kind_reaches_postgresql(runtime, tmp_path):
    database, goals, plans, goal = runtime
    task = plans.create_task(goal.goal_id, "full sweep")
    TaskQueue(plans).ready(goal.goal_id)

    Scheduler(database, plans).add(task.task_id, trigger_type=TriggerType.TIME)
    ApprovalQueue(database, plans).request(task.task_id, action="sweep", reason="verification",
                                           expected_effect="none", rollback_plan="cascade delete")
    TaskLeaseManager(database, plans).acquire(task.task_id, "blanc", ttl_seconds=60)
    HeartbeatManager(database, plans).beat(task.task_id, progress_marker="1%", current_step="sweep")
    AutonomyBudgetTracker(database, goals).consume(goal.goal_id, tool_calls=1)
    FailureBudgetManager(database, goals).record(goal.goal_id, "provider_failure",
                                                 limits={"provider_failure": 5})
    plans.transition(task.task_id, GoalTaskStatus.RUNNING)
    ResumeManager(database, plans, goals).pause_goal(goal.goal_id,
                                                     checkpoint_ids={task.task_id: "cp-sweep"})
    target = tmp_path / "sweep.txt"
    target.write_text("sweep\n", encoding="utf-8")
    ArtifactTracker(database, tmp_path).register(goal.goal_id, task.task_id, target.name,
                                                 artifact_type="text", version="1")

    kinds = ["approval", "task_lease", "heartbeat", "autonomy_usage", "artifact",
             "failure_budget", "resume_point", "schedule"]
    written = {kind: len(database.list_goal_runtime_records(kind, goal_id=goal.goal_id))
               for kind in kinds}
    assert all(count > 0 for count in written.values()), written


@pytest.mark.xfail(reason="dependency_task_id lacks ON DELETE CASCADE; no product API deletes "
                          "goals yet, so this is latent. Fix requires a constraint migration "
                          "before any goal archive/delete path ships.",
                   raises=Exception, strict=True)
def test_goal_delete_cascades_through_dependency_edges(runtime):
    """Documents a real divergence the SQLite suite structurally cannot catch.

    SQLite never enables ``PRAGMA foreign_keys``, so its identical declaration is
    inert; PostgreSQL enforces it and refuses the delete.
    """
    database, goals, plans, goal = runtime
    first = plans.create_task(goal.goal_id, "first")
    plans.create_task(goal.goal_id, "second", dependencies=[first.task_id])

    with database.selected()._connect() as db, db.cursor() as cursor:
        cursor.execute("DELETE FROM runtime.goals WHERE goal_id = %s", (goal.goal_id,))
        db.commit()
