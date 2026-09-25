from __future__ import annotations

import sqlite3

import pytest

from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.cli.sophia import _goal_command
from sophia.core.orchestrator import SophiaCore
from sophia.database import DatabaseManager
from sophia.goals import (
    AutonomyBudget, GoalManager, GoalStatus, GoalTaskStatus, InvalidGoalTransition,
    PlanManager, PriorityManager, TaskQueue,
)


def _manager(tmp_path) -> tuple[GoalManager, DatabaseManager]:
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    return GoalManager(database), database


def test_goal_create_requires_explicit_success_criteria(tmp_path):
    manager, _ = _manager(tmp_path)
    with pytest.raises(ValueError, match="success_criteria"):
        manager.create("Never ambiguous", success_criteria=[])


def test_goal_round_trip_preserves_policy_and_budget(tmp_path):
    manager, database = _manager(tmp_path)
    budget = AutonomyBudget(max_runtime_minutes=30, max_tool_calls=12, max_r2_actions=2)
    created = manager.create(
        "Ship v0.9 Phase A",
        success_criteria=["goal persistence", "lifecycle tests"],
        constraints=["no scheduler", "R3 requires approval"],
        priority=80,
        risk_ceiling="r2",
        autonomy_budget=budget,
    )

    restored = GoalManager(database).get(created.goal_id)
    assert restored == created
    assert restored.risk_ceiling == "R2"
    assert restored.autonomy_budget == budget
    assert restored.progress == 0.0


def test_goal_lifecycle_cannot_complete_before_criteria(tmp_path):
    manager, _ = _manager(tmp_path)
    goal = manager.create("Verified delivery", success_criteria=["tests pass", "diff clean"], activate=True)
    assert goal.status is GoalStatus.ACTIVE

    with pytest.raises(InvalidGoalTransition, match="incomplete"):
        manager.complete(goal.goal_id)
    goal = manager.satisfy(goal.goal_id, "tests pass")
    assert goal.progress == 0.5
    goal = manager.satisfy(goal.goal_id, "diff clean")
    assert manager.complete(goal.goal_id).status is GoalStatus.COMPLETED
    with pytest.raises(InvalidGoalTransition):
        manager.resume(goal.goal_id)


def test_pause_resume_cancel_and_status_filter(tmp_path):
    manager, _ = _manager(tmp_path)
    first = manager.create("First", success_criteria=["done"], priority=10, activate=True)
    second = manager.create("Second", success_criteria=["done"], priority=90)
    assert manager.pause(first.goal_id).status is GoalStatus.PAUSED
    assert manager.resume(first.goal_id).status is GoalStatus.ACTIVE
    assert manager.cancel(second.goal_id).status is GoalStatus.CANCELLED
    assert [goal.goal_id for goal in manager.list(status=GoalStatus.ACTIVE)] == [first.goal_id]


def test_goal_write_and_audit_event_share_the_sqlite_transaction(tmp_path):
    manager, database = _manager(tmp_path)
    goal = manager.create("Audited", success_criteria=["trace exists"], activate=True)
    path = database.backends["sqlite"].path
    with sqlite3.connect(path) as connection:
        events = connection.execute(
            "SELECT event_type FROM sophia_goal_events WHERE goal_id = ? ORDER BY event_id", (goal.goal_id,),
        ).fetchall()
    assert events == [("goal_created",), ("goal_activated",)]


def test_core_exposes_manual_goal_runtime_and_cli_commands(tmp_path):
    registry = BrainRegistry()
    registry.register("general", DummyBrain("ok"))
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl", database=database)

    output = _goal_command(core, 'create "CLI goal" --criteria "visible status" --activate')
    goal_id = output.split()[1]
    assert "ACTIVE" in output
    assert "CLI goal" in _goal_command(core, "list")
    assert "visible status" in _goal_command(core, f"status {goal_id}")
    assert "[1/1]" in _goal_command(core, f'satisfy {goal_id} "visible status"')
    assert "COMPLETED" in _goal_command(core, f"complete {goal_id}")


def test_plan_rejects_unknown_dependency_and_goal_risk_overflow(tmp_path):
    goals, database = _manager(tmp_path)
    goal = goals.create("Safe plan", success_criteria=["done"], risk_ceiling="R1")
    plans = PlanManager(database)
    with pytest.raises(ValueError, match="unknown dependency"):
        plans.create_task(goal.goal_id, "Invalid", dependencies=["ltask-missing"])
    with pytest.raises(PermissionError, match="exceeds goal ceiling"):
        plans.create_task(goal.goal_id, "Too risky", risk="R2")


def test_task_queue_contains_dependency_ready_tasks_only(tmp_path):
    goals, database = _manager(tmp_path)
    goal = goals.create("Ordered plan", success_criteria=["all tasks complete"], priority=50, activate=True)
    plans = PlanManager(database)
    first = plans.create_task(goal.goal_id, "Build", priority=20)
    second = plans.create_task(goal.goal_id, "Test", dependencies=[first.task_id], priority=100)
    gated = plans.create_task(goal.goal_id, "Deploy", approval_requirement=True)
    queue = TaskQueue(plans)

    assert [task.task_id for task in queue.ready(goal.goal_id)] == [first.task_id]
    plans.transition(first.task_id, GoalTaskStatus.COMPLETED)
    assert [task.task_id for task in queue.ready(goal.goal_id)] == [second.task_id]
    assert plans.get_task(gated.task_id).status is GoalTaskStatus.PENDING


def test_queue_orders_by_goal_task_blocking_and_risk_weights(tmp_path):
    goals, database = _manager(tmp_path)
    goal = goals.create("Priority", success_criteria=["done"], priority=40, risk_ceiling="R2", activate=True)
    plans = PlanManager(database)
    blocker = plans.create_task(goal.goal_id, "Unblocks work", priority=10, risk="R1")
    plans.create_task(goal.goal_id, "Dependent", dependencies=[blocker.task_id])
    independent = plans.create_task(goal.goal_id, "Independent", priority=15, risk="R2")

    ready = TaskQueue(plans).ready(goal.goal_id)
    assert [task.task_id for task in ready] == [blocker.task_id, independent.task_id]
    assert PriorityManager.score(blocker, goal_priority=40, blocking_count=1) > PriorityManager.score(
        independent, goal_priority=40,
    )


def test_inactive_goal_never_exposes_queue_work(tmp_path):
    goals, database = _manager(tmp_path)
    goal = goals.create("Draft", success_criteria=["done"])
    plans = PlanManager(database)
    plans.create_task(goal.goal_id, "Wait")
    assert TaskQueue(plans).ready(goal.goal_id) == []
