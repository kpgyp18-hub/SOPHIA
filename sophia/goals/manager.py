from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable

from sophia.database import DatabaseManager
from sophia.goals.models import AutonomyBudget, Goal, GoalStatus


class InvalidGoalTransition(ValueError):
    """Raised when a manual goal action violates the v0.9 lifecycle."""


_ALLOWED: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.DRAFT: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLED}),
    GoalStatus.ACTIVE: frozenset({
        GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.COMPLETED,
        GoalStatus.FAILED, GoalStatus.CANCELLED,
    }),
    GoalStatus.PAUSED: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLED}),
    GoalStatus.BLOCKED: frozenset({GoalStatus.ACTIVE, GoalStatus.FAILED, GoalStatus.CANCELLED}),
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}


class GoalManager:
    """Manual v0.9 goal lifecycle; it never schedules or executes tasks."""

    def __init__(self, database: DatabaseManager) -> None:
        self.database = database

    def create(self, title: str, *, success_criteria: Iterable[str], description: str = "",
               owner: str = "user", target_date: str | None = None, priority: int = 0,
               constraints: Iterable[str] = (), risk_ceiling: str = "R1",
               autonomy_budget: AutonomyBudget | None = None, activate: bool = False) -> Goal:
        goal = Goal(
            title=title,
            description=description,
            owner=owner,
            target_date=target_date,
            priority=priority,
            success_criteria=tuple(success_criteria),
            constraints=tuple(constraints),
            risk_ceiling=risk_ceiling,
            autonomy_budget=autonomy_budget or AutonomyBudget(),
        )
        self._save(goal, "goal_created")
        return self.activate(goal.goal_id) if activate else goal

    def get(self, goal_id: str) -> Goal:
        record = self.database.get_goal(goal_id)
        if record is None:
            raise KeyError(goal_id)
        return Goal.from_record(record)

    def list(self, *, status: GoalStatus | str | None = None) -> list[Goal]:
        normalized = GoalStatus(status).value if status is not None else None
        return [Goal.from_record(record) for record in self.database.list_goals(status=normalized)]

    def activate(self, goal_id: str) -> Goal:
        return self._transition(goal_id, GoalStatus.ACTIVE, "goal_activated")

    def pause(self, goal_id: str) -> Goal:
        return self._transition(goal_id, GoalStatus.PAUSED, "goal_paused")

    def resume(self, goal_id: str) -> Goal:
        return self._transition(goal_id, GoalStatus.ACTIVE, "goal_resumed")

    def block(self, goal_id: str) -> Goal:
        return self._transition(goal_id, GoalStatus.BLOCKED, "goal_blocked")

    def fail(self, goal_id: str) -> Goal:
        return self._transition(goal_id, GoalStatus.FAILED, "goal_failed")

    def cancel(self, goal_id: str) -> Goal:
        return self._transition(goal_id, GoalStatus.CANCELLED, "goal_cancelled")

    def satisfy(self, goal_id: str, criterion: str) -> Goal:
        goal = self.get(goal_id)
        normalized = criterion.strip()
        if normalized not in goal.success_criteria:
            raise ValueError(f"criterion is not declared for {goal_id}: {criterion}")
        if normalized in goal.satisfied_criteria:
            return goal
        updated = replace(
            goal,
            satisfied_criteria=(*goal.satisfied_criteria, normalized),
            updated_at=self._now(),
        )
        self._save(updated, "goal_criterion_satisfied", {"criterion": normalized})
        return updated

    def complete(self, goal_id: str) -> Goal:
        goal = self.get(goal_id)
        if not goal.completion_ready:
            missing = sorted(set(goal.success_criteria) - set(goal.satisfied_criteria))
            raise InvalidGoalTransition(f"goal success criteria are incomplete: {missing}")
        return self._transition(goal_id, GoalStatus.COMPLETED, "goal_completed")

    def _transition(self, goal_id: str, target: GoalStatus, event_type: str) -> Goal:
        goal = self.get(goal_id)
        if target not in _ALLOWED[goal.status]:
            raise InvalidGoalTransition(f"invalid goal transition: {goal.status.value} -> {target.value}")
        updated = replace(goal, status=target, updated_at=self._now())
        self._save(updated, event_type, {"from": goal.status.value, "to": target.value})
        return updated

    def _save(self, goal: Goal, event_type: str, payload: dict[str, str] | None = None) -> None:
        self.database.save_goal(goal.to_record(), event_type=event_type, event_payload=payload or {})

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
