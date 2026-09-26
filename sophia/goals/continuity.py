from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sophia.database import DatabaseManager
from sophia.goals.manager import GoalManager
from sophia.goals.models import GoalStatus
from sophia.goals.planning import GoalTask, GoalTaskStatus, PlanManager


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ResumePoint:
    goal_id: str
    task_id: str | None
    checkpoint_id: str | None
    state_snapshot: Mapping[str, Any]
    open_decisions: tuple[str, ...] = ()
    approval_state: Mapping[str, Any] = field(default_factory=dict)
    resume_id: str = field(default_factory=lambda: f"resume-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=utc_now)
    resumed_at: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "resume_id": self.resume_id, "goal_id": self.goal_id, "task_id": self.task_id,
            "checkpoint_id": self.checkpoint_id, "state_snapshot": dict(self.state_snapshot),
            "open_decisions": list(self.open_decisions), "approval_state": dict(self.approval_state),
            "created_at": self.created_at, "updated_at": self.resumed_at or self.created_at,
            "resumed_at": self.resumed_at,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> ResumePoint:
        return cls(
            resume_id=str(value["resume_id"]), goal_id=str(value["goal_id"]),
            task_id=str(value["task_id"]) if value.get("task_id") else None,
            checkpoint_id=str(value["checkpoint_id"]) if value.get("checkpoint_id") else None,
            state_snapshot=dict(value.get("state_snapshot", {})),
            open_decisions=tuple(value.get("open_decisions", ())),
            approval_state=dict(value.get("approval_state", {})),
            created_at=str(value["created_at"]),
            resumed_at=str(value["resumed_at"]) if value.get("resumed_at") else None,
        )


class ResumeManager:
    """Persist continuation state; resuming only re-queues work and never executes it."""

    KIND = "resume_point"

    def __init__(self, database: DatabaseManager, plans: PlanManager,
                 goals: GoalManager | None = None) -> None:
        self.database, self.plans = database, plans
        self.goals = goals or GoalManager(database)

    def checkpoint(self, goal_id: str, *, task_id: str | None = None,
                   checkpoint_id: str | None = None,
                   state_snapshot: Mapping[str, Any] | None = None,
                   open_decisions: tuple[str, ...] = (),
                   approval_state: Mapping[str, Any] | None = None) -> ResumePoint:
        self.goals.get(goal_id)
        if task_id is not None and self.plans.get_task(task_id).goal_id != goal_id:
            raise ValueError("resume task does not belong to goal")
        point = ResumePoint(
            goal_id=goal_id, task_id=task_id, checkpoint_id=checkpoint_id,
            state_snapshot=dict(state_snapshot or {}), open_decisions=open_decisions,
            approval_state=dict(approval_state or {}),
        )
        self._save(point)
        self.database.record("goal.checkpoint_created", point.to_record())
        return point

    def get(self, resume_id: str) -> ResumePoint:
        record = self.database.get_goal_runtime_record(self.KIND, resume_id)
        if record is None:
            raise KeyError(resume_id)
        return ResumePoint.from_record(record)

    def latest(self, goal_id: str, *, task_id: str | None = None) -> ResumePoint | None:
        records = self.database.list_goal_runtime_records(self.KIND, goal_id=goal_id, task_id=task_id)
        if not records:
            return None
        return ResumePoint.from_record(max(records, key=lambda item: str(item["created_at"])))

    def pause_goal(self, goal_id: str, *, checkpoint_ids: Mapping[str, str] | None = None) -> list[ResumePoint]:
        points: list[ResumePoint] = []
        for task in self.plans.list_tasks(goal_id):
            if task.status not in {GoalTaskStatus.READY, GoalTaskStatus.RUNNING, GoalTaskStatus.WAITING}:
                continue
            paused = self.plans.transition(task.task_id, GoalTaskStatus.PAUSED)
            points.append(self.checkpoint(
                goal_id, task_id=task.task_id,
                checkpoint_id=(checkpoint_ids or {}).get(task.task_id),
                state_snapshot={"task_status": task.status.value, "resume_status": "pending",
                                "updated_at": paused.updated_at},
            ))
        if self.goals.get(goal_id).status is GoalStatus.ACTIVE:
            self.goals.pause(goal_id)
        return points

    def resume(self, resume_id: str) -> GoalTask | None:
        point = self.get(resume_id)
        if point.resumed_at is not None:
            return self.plans.get_task(point.task_id) if point.task_id else None
        task = self.plans.get_task(point.task_id) if point.task_id else None
        if task is not None:
            if task.status is not GoalTaskStatus.PAUSED:
                raise ValueError("resume requires a paused task; retry is a separate operation")
            task = self.plans.transition(task.task_id, GoalTaskStatus.PENDING)
        if self.goals.get(point.goal_id).status is GoalStatus.PAUSED:
            self.goals.resume(point.goal_id)
        self._save(ResumePoint(**{**point.__dict__, "resumed_at": utc_now()}))
        self.database.record("goal.resume", {"resume_id": resume_id, "goal_id": point.goal_id,
                                             "task_id": point.task_id})
        return task

    def packet(self, goal_id: str) -> dict[str, Any]:
        goal = self.goals.get(goal_id)
        tasks = self.plans.list_tasks(goal_id)
        latest = self.latest(goal_id)
        completed = [task for task in tasks if task.status is GoalTaskStatus.COMPLETED]
        active = [task for task in tasks if task.status not in {
            GoalTaskStatus.COMPLETED, GoalTaskStatus.CANCELLED, GoalTaskStatus.FAILED,
        }]
        return {
            "goal": goal.to_record(),
            "last_completed_task": completed[-1].to_record() if completed else None,
            "active_tasks": [task.to_record() for task in active],
            "open_decisions": list(latest.open_decisions) if latest else [],
            "latest_checkpoint": latest.to_record() if latest else None,
        }

    def _save(self, point: ResumePoint) -> None:
        self.database.save_goal_runtime_record(
            self.KIND, point.resume_id, point.to_record(), goal_id=point.goal_id,
            task_id=point.task_id, status="resumed" if point.resumed_at else "active",
        )


@dataclass(frozen=True)
class RecoveryDecision:
    classification: str
    recoverable: bool
    action: str
    resume_id: str | None = None


class RecoveryManager:
    """Classify a failure and fail closed when no trustworthy checkpoint exists."""

    RECOVERABLE = frozenset({"provider_failure", "resource_shortage", "process_restart", "tool_failure"})

    def __init__(self, resume: ResumeManager) -> None:
        self.resume = resume

    def recover(self, goal_id: str, task_id: str, *, classification: str,
                checkpoint_id: str | None, state_snapshot: Mapping[str, Any] | None = None) -> RecoveryDecision:
        task = self.resume.plans.get_task(task_id)
        recoverable = classification in self.RECOVERABLE and checkpoint_id is not None
        if recoverable:
            if task.status in {GoalTaskStatus.RUNNING, GoalTaskStatus.READY, GoalTaskStatus.WAITING}:
                self.resume.plans.transition(task_id, GoalTaskStatus.PAUSED)
            point = self.resume.checkpoint(goal_id, task_id=task_id, checkpoint_id=checkpoint_id,
                                           state_snapshot=state_snapshot or {})
            return RecoveryDecision(classification, True, "resume", point.resume_id)
        if task.status in {GoalTaskStatus.PENDING, GoalTaskStatus.WAITING}:
            self.resume.plans.transition(task_id, GoalTaskStatus.BLOCKED)
        elif task.status in {GoalTaskStatus.READY, GoalTaskStatus.RUNNING}:
            if task.status is GoalTaskStatus.RUNNING:
                self.resume.plans.transition(task_id, GoalTaskStatus.PAUSED)
                self.resume.plans.transition(task_id, GoalTaskStatus.PENDING)
            self.resume.plans.transition(task_id, GoalTaskStatus.BLOCKED)
        self.resume.database.record("goal.recovery_blocked", {
            "goal_id": goal_id, "task_id": task_id, "classification": classification,
            "checkpoint_id": checkpoint_id,
        })
        return RecoveryDecision(classification, False, "blocked")


class ProgressTracker:
    def __init__(self, goals: GoalManager, plans: PlanManager) -> None:
        self.goals, self.plans = goals, plans

    def snapshot(self, goal_id: str) -> dict[str, Any]:
        goal = self.goals.get(goal_id)
        tasks = self.plans.list_tasks(goal_id)
        total = len(tasks)
        completed = sum(task.status is GoalTaskStatus.COMPLETED for task in tasks)
        blocked = sum(task.status is GoalTaskStatus.BLOCKED for task in tasks)
        failed = sum(task.status is GoalTaskStatus.FAILED for task in tasks)
        confidence = "high" if total and not blocked and not failed else "medium" if total else "low"
        return {
            "goal_id": goal_id, "goal_progress": goal.progress,
            "task_progress": completed / total if total else 0.0,
            "task_total": total, "completed_count": completed,
            "blocked_count": blocked, "failure_count": failed,
            "eta_confidence": confidence,
        }


class GoalConflictDetector:
    """Detect explicit contradictory constraints without model inference."""

    @staticmethod
    def conflicts(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str, ...]:
        left_constraints = {str(item).strip().lower() for item in left.get("constraints", ())}
        right_constraints = {str(item).strip().lower() for item in right.get("constraints", ())}
        pairs = (("local-only", "external-provider"), ("offline", "network-required"))
        found: list[str] = []
        for first, second in pairs:
            if ((first in left_constraints and second in right_constraints) or
                    (second in left_constraints and first in right_constraints)):
                found.append(f"{first} conflicts with {second}")
        return tuple(found)
