from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sophia.database import DatabaseManager
from sophia.goals import (
    ApprovalQueue, ArtifactState, ArtifactTracker, AutonomyBudget, AutonomyBudgetTracker,
    BackgroundHealthMonitor, BackgroundRuntime, DeadlineManager, FailureBudgetManager,
    GoalConflictDetector, GoalManager, GoalStatus, GoalTaskStatus, HealthState,
    HeartbeatManager, PlanManager, ProgressTracker, RecoveryManager, ResumeManager,
    RuntimeHealth, Scheduler, TaskLeaseManager, TaskQueue, TriggerType, Watchdog,
)
from sophia.runtime.tool_registry import ToolRegistry, ToolSpec
from sophia.runtime.tools import ToolGateway


def _runtime(tmp_path, *, risk: str = "R2", budget: AutonomyBudget | None = None):
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    goals = GoalManager(database)
    goal = goals.create("Long goal", success_criteria=["all done"], risk_ceiling=risk,
                        autonomy_budget=budget or AutonomyBudget(), activate=True)
    plans = PlanManager(database)
    return database, goals, goal, plans


def test_pause_resume_packet_survives_manager_restart_and_is_not_retry(tmp_path):
    database, goals, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Checkpoint me")
    TaskQueue(plans).ready(goal.goal_id)
    plans.transition(task.task_id, GoalTaskStatus.RUNNING)
    resume = ResumeManager(database, plans, goals)

    points = resume.pause_goal(goal.goal_id, checkpoint_ids={task.task_id: "cp-1"})
    assert goals.get(goal.goal_id).status is GoalStatus.PAUSED
    assert plans.get_task(task.task_id).status is GoalTaskStatus.PAUSED

    restored = ResumeManager(database, PlanManager(database), GoalManager(database))
    packet = restored.packet(goal.goal_id)
    assert packet["latest_checkpoint"]["checkpoint_id"] == "cp-1"
    assert restored.resume(points[0].resume_id).status is GoalTaskStatus.PENDING
    assert restored.resume(points[0].resume_id).status is GoalTaskStatus.PENDING
    assert GoalManager(database).get(goal.goal_id).status is GoalStatus.ACTIVE


def test_recovery_requires_checkpoint_and_separates_resume_from_retry(tmp_path):
    database, goals, goal, plans = _runtime(tmp_path)
    recoverable = plans.create_task(goal.goal_id, "Provider outage")
    TaskQueue(plans).ready(goal.goal_id)
    plans.transition(recoverable.task_id, GoalTaskStatus.RUNNING)
    recovery = RecoveryManager(ResumeManager(database, plans, goals))

    decision = recovery.recover(goal.goal_id, recoverable.task_id,
                                classification="provider_failure", checkpoint_id="cp-ok")
    assert decision.recoverable and decision.action == "resume"
    assert plans.get_task(recoverable.task_id).status is GoalTaskStatus.PAUSED

    blocked = plans.create_task(goal.goal_id, "Partial write")
    decision = recovery.recover(goal.goal_id, blocked.task_id,
                                classification="partial_write", checkpoint_id=None)
    assert not decision.recoverable and decision.action == "blocked"
    assert plans.get_task(blocked.task_id).status is GoalTaskStatus.BLOCKED


def test_progress_confidence_and_goal_conflict(tmp_path):
    _, goals, goal, plans = _runtime(tmp_path)
    done = plans.create_task(goal.goal_id, "Done")
    plans.transition(done.task_id, GoalTaskStatus.READY)
    plans.transition(done.task_id, GoalTaskStatus.COMPLETED)
    plans.create_task(goal.goal_id, "Remaining")
    snapshot = ProgressTracker(goals, plans).snapshot(goal.goal_id)
    assert snapshot["task_progress"] == 0.5
    assert snapshot["eta_confidence"] == "high"
    assert GoalConflictDetector.conflicts(
        {"constraints": ["local-only"]}, {"constraints": ["external-provider"]},
    ) == ("local-only conflicts with external-provider",)


def test_scheduler_wakes_only_r0_r1_and_never_executes(tmp_path):
    database, _, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Health check", risk="R1")
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    scheduler = Scheduler(database, plans, clock=lambda: now)
    schedule = scheduler.add(task.task_id, next_run=(now - timedelta(seconds=1)).isoformat())

    ready = scheduler.due()
    assert [item.task_id for item in ready] == [task.task_id]
    assert plans.get_task(task.task_id).status is GoalTaskStatus.READY
    assert not scheduler.get(schedule.schedule_id).enabled

    risky = plans.create_task(goal.goal_id, "Needs foreground", risk="R2")
    with pytest.raises(PermissionError, match="R0/R1"):
        scheduler.add(risky.task_id)


def test_resource_wait_returns_to_ready_without_blind_retry(tmp_path):
    _, _, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Needs GPU")
    available = False
    queue = TaskQueue(plans, resource_available=lambda _task: available)
    assert queue.ready(goal.goal_id) == []
    assert plans.get_task(task.task_id).status is GoalTaskStatus.WAITING
    available = True
    assert [item.task_id for item in queue.ready(goal.goal_id)] == [task.task_id]


def test_dependency_trigger_and_deadline_alert(tmp_path):
    database, _, goal, plans = _runtime(tmp_path)
    first = plans.create_task(goal.goal_id, "First")
    second = plans.create_task(
        goal.goal_id, "Second", dependencies=[first.task_id],
        deadline=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    scheduler = Scheduler(database, plans)
    scheduler.add(second.task_id, trigger_type=TriggerType.DEPENDENCY)
    assert scheduler.due() == []
    plans.transition(first.task_id, GoalTaskStatus.READY)
    plans.transition(first.task_id, GoalTaskStatus.COMPLETED)
    assert [item.task_id for item in scheduler.due()] == [second.task_id]
    assert DeadlineManager(plans).alerts(goal.goal_id, warning_seconds=600)[0]["type"] == "USER_ALERT"


def test_periodic_schedule_creates_a_fresh_task_after_completion(tmp_path):
    database, _, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Periodic health")
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    scheduler = Scheduler(database, plans, clock=lambda: now)
    schedule = scheduler.add(task.task_id, next_run=now.isoformat(), interval_seconds=60)
    first = scheduler.due()[0]
    plans.transition(first.task_id, GoalTaskStatus.COMPLETED)
    now += timedelta(seconds=60)
    second = scheduler.due()[0]
    assert second.task_id != first.task_id
    assert scheduler.get(schedule.schedule_id).task_id == second.task_id


def test_approval_queue_is_user_decided_and_unblocks_queue(tmp_path):
    database, _, goal, plans = _runtime(tmp_path, risk="R4")
    task = plans.create_task(goal.goal_id, "Approved write", risk="R2", approval_requirement=True)
    approvals = ApprovalQueue(database, plans)
    queue = TaskQueue(plans, approval_granted=approvals.granted)
    assert queue.ready(goal.goal_id) == []
    request = approvals.request(task.task_id, action="write config", reason="required",
                                expected_effect="updated config", rollback_plan="restore checkpoint")
    with pytest.raises(PermissionError, match="only the user"):
        approvals.approve(request.approval_id, actor="scheduler")
    approvals.approve(request.approval_id, actor="user")
    assert [item.task_id for item in queue.ready(goal.goal_id)] == [task.task_id]

    with pytest.raises(ValueError, match="require approval"):
        plans.create_task(goal.goal_id, "Destructive", risk="R3")


def test_retry_is_explicit_and_distinct_from_resume(tmp_path):
    _, _, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Retry me")
    plans.transition(task.task_id, GoalTaskStatus.READY)
    plans.transition(task.task_id, GoalTaskStatus.RUNNING)
    plans.transition(task.task_id, GoalTaskStatus.FAILED)
    assert plans.retry(task.task_id).status is GoalTaskStatus.PENDING
    with pytest.raises(ValueError, match="only a failed"):
        plans.retry(task.task_id)


def test_task_lease_heartbeat_and_zombie_detection(tmp_path):
    database, _, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Leased")
    plans.transition(task.task_id, GoalTaskStatus.READY)
    plans.transition(task.task_id, GoalTaskStatus.RUNNING)
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    leases = TaskLeaseManager(database, plans, clock=lambda: now)
    lease = leases.acquire(task.task_id, "worker-a", ttl_seconds=30)
    with pytest.raises(RuntimeError, match="leased"):
        leases.acquire(task.task_id, "worker-b")
    heartbeats = HeartbeatManager(database, plans, clock=lambda: now)
    heartbeats.beat(task.task_id, progress_marker="1/2", current_step="read")
    watchdog = Watchdog(plans, heartbeats, clock=lambda: now + timedelta(seconds=61))
    assert watchdog.inspect(goal.goal_id, timeout_seconds=60) == [task.task_id]
    assert plans.get_task(task.task_id).status is GoalTaskStatus.PAUSED
    leases.release(lease.lease_id, owner="worker-a")


def test_background_runtime_uses_tool_registry_and_is_idempotent(tmp_path):
    budget = AutonomyBudget(max_runtime_minutes=10, max_tool_calls=2)
    database, goals, goal, plans = _runtime(tmp_path, risk="R1", budget=budget)
    task = plans.create_task(goal.goal_id, "Read-only maintenance", risk="R1")
    TaskQueue(plans).ready(goal.goal_id)
    tools = ToolRegistry(ToolGateway(tmp_path), specs=())
    tools.register(ToolSpec("maintenance.check", "check", "system", "R1", 10, False,
                            False, False, frozenset({"aide"}), True))
    leases = TaskLeaseManager(database, plans)
    beats = HeartbeatManager(database, plans)
    runtime = BackgroundRuntime(plans, tools, leases, beats, AutonomyBudgetTracker(database, goals))
    calls: list[str] = []

    result = runtime.run_once(
        task.task_id, worker_id="worker-a", tool_name="maintenance.check",
        idempotency_key="one", handler=lambda invocation: calls.append(invocation.spec.name) or "ok",
    )
    assert result == "ok" and calls == ["maintenance.check"]
    assert plans.get_task(task.task_id).status is GoalTaskStatus.COMPLETED
    assert AutonomyBudgetTracker(database, goals).usage(goal.goal_id)["tool_calls"] == 1


def test_background_rejects_r2_even_with_registered_handler(tmp_path):
    database, goals, goal, plans = _runtime(tmp_path, risk="R2")
    task = plans.create_task(goal.goal_id, "Unsafe background", risk="R2")
    plans.transition(task.task_id, GoalTaskStatus.READY)
    tools = ToolRegistry(ToolGateway(tmp_path), specs=())
    tools.register(ToolSpec("maintenance.write", "write", "system", "R2", 10, False,
                            False, False, frozenset({"aide"}), True))
    runtime = BackgroundRuntime(
        plans, tools, TaskLeaseManager(database, plans), HeartbeatManager(database, plans),
        AutonomyBudgetTracker(database, goals),
    )
    with pytest.raises(PermissionError, match="R0/R1"):
        runtime.run_once(task.task_id, worker_id="worker", tool_name="maintenance.write",
                         handler=lambda _invocation: "should not run")


def test_background_rejects_network_even_when_declared_r1(tmp_path):
    database, goals, goal, plans = _runtime(tmp_path, risk="R1")
    task = plans.create_task(goal.goal_id, "External probe", risk="R1")
    plans.transition(task.task_id, GoalTaskStatus.READY)
    tools = ToolRegistry(ToolGateway(tmp_path), specs=())
    tools.register(ToolSpec("maintenance.network", "probe", "network", "R1", 10, False,
                            False, False, frozenset({"aide"}), True, network_access=True))
    runtime = BackgroundRuntime(
        plans, tools, TaskLeaseManager(database, plans), HeartbeatManager(database, plans),
        AutonomyBudgetTracker(database, goals),
    )
    with pytest.raises(PermissionError, match="network or secrets"):
        runtime.run_once(task.task_id, worker_id="worker", tool_name="maintenance.network",
                         handler=lambda _invocation: "should not run")


def test_autonomy_and_failure_budgets_pause_goal(tmp_path):
    budget = AutonomyBudget(max_runtime_minutes=1, max_tool_calls=1)
    database, goals, goal, _ = _runtime(tmp_path, budget=budget)
    tracker = AutonomyBudgetTracker(database, goals)
    tracker.consume(goal.goal_id, tool_calls=1)
    with pytest.raises(RuntimeError, match="budget exceeded"):
        tracker.consume(goal.goal_id, tool_calls=1)
    state = FailureBudgetManager(database, goals).record(
        goal.goal_id, "provider_failures", limits={"provider_failures": 0},
    )
    assert state["degraded"]
    assert goals.get(goal.goal_id).status is GoalStatus.PAUSED


def test_health_monitor_and_artifact_tracking(tmp_path):
    database, _, goal, plans = _runtime(tmp_path)
    task = plans.create_task(goal.goal_id, "Artifact")
    artifact = tmp_path / "report.txt"
    artifact.write_text("verified", encoding="utf-8")
    record = ArtifactTracker(database, tmp_path).register(
        goal.goal_id, task.task_id, "report.txt", artifact_type="text", version="1",
        state=ArtifactState.VALIDATED,
    )
    assert record["status"] == "validated" and len(record["hash"]) == 64
    with pytest.raises(PermissionError, match="escapes"):
        ArtifactTracker(database, tmp_path).register(
            goal.goal_id, task.task_id, "../outside.txt", artifact_type="text", version="1",
        )

    monitor = BackgroundHealthMonitor()
    monitor.register("queue", lambda: RuntimeHealth("queue", HealthState.HEALTHY, "ready"))
    monitor.register("broken", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert [item.state for item in monitor.check()] == [HealthState.HEALTHY, HealthState.OFFLINE]
