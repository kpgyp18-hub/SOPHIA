from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from sophia.database import DatabaseManager
from sophia.goals.models import GoalStatus


class GoalTaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


@dataclass(frozen=True)
class GoalTask:
    goal_id: str
    title: str
    description: str = ""
    parent_task_id: str | None = None
    priority: int = 0
    status: GoalTaskStatus = GoalTaskStatus.PENDING
    dependencies: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    risk: str = "R1"
    approval_requirement: bool = False
    deadline: str | None = None
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_policy: Mapping[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: f"ltask-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("task title is required")
        risk = self.risk.upper()
        if risk not in {"R0", "R1", "R2", "R3", "R4"}:
            raise ValueError(f"unsupported task risk: {self.risk}")
        if not 0 <= self.priority <= 100:
            raise ValueError("task priority must be between 0 and 100")
        dependencies = tuple(dict.fromkeys(item for item in self.dependencies if item))
        if self.task_id in dependencies:
            raise ValueError("task cannot depend on itself")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "required_skills", tuple(dict.fromkeys(self.required_skills)))
        object.__setattr__(self, "required_capabilities", tuple(dict.fromkeys(self.required_capabilities)))
        object.__setattr__(self, "retry_policy", dict(self.retry_policy))
        object.__setattr__(self, "checkpoint_policy", dict(self.checkpoint_policy))

    def to_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "goal_id": self.goal_id, "parent_task_id": self.parent_task_id,
            "title": self.title, "description": self.description, "priority": self.priority,
            "status": self.status.value, "dependencies": list(self.dependencies),
            "required_skills": list(self.required_skills),
            "required_capabilities": list(self.required_capabilities), "risk": self.risk,
            "approval_requirement": self.approval_requirement, "deadline": self.deadline,
            "retry_policy": dict(self.retry_policy), "checkpoint_policy": dict(self.checkpoint_policy),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> GoalTask:
        return cls(
            task_id=str(value["task_id"]), goal_id=str(value["goal_id"]),
            parent_task_id=value.get("parent_task_id"), title=str(value["title"]),
            description=str(value.get("description", "")), priority=int(value.get("priority", 0)),
            status=GoalTaskStatus(str(value.get("status", "pending"))),
            dependencies=tuple(value.get("dependencies", ())),
            required_skills=tuple(value.get("required_skills", ())),
            required_capabilities=tuple(value.get("required_capabilities", ())),
            risk=str(value.get("risk", "R1")),
            approval_requirement=bool(value.get("approval_requirement", False)),
            deadline=value.get("deadline"), retry_policy=value.get("retry_policy", {}),
            checkpoint_policy=value.get("checkpoint_policy", {}),
            created_at=str(value["created_at"]), updated_at=str(value["updated_at"]),
        )


_TASK_TRANSITIONS: dict[GoalTaskStatus, frozenset[GoalTaskStatus]] = {
    GoalTaskStatus.PENDING: frozenset({GoalTaskStatus.READY, GoalTaskStatus.BLOCKED, GoalTaskStatus.CANCELLED}),
    GoalTaskStatus.READY: frozenset({GoalTaskStatus.RUNNING, GoalTaskStatus.COMPLETED, GoalTaskStatus.PAUSED, GoalTaskStatus.CANCELLED}),
    GoalTaskStatus.RUNNING: frozenset({GoalTaskStatus.REVIEW, GoalTaskStatus.COMPLETED, GoalTaskStatus.FAILED, GoalTaskStatus.PAUSED}),
    GoalTaskStatus.WAITING: frozenset({GoalTaskStatus.READY, GoalTaskStatus.BLOCKED, GoalTaskStatus.CANCELLED}),
    GoalTaskStatus.BLOCKED: frozenset({GoalTaskStatus.PENDING, GoalTaskStatus.CANCELLED}),
    GoalTaskStatus.REVIEW: frozenset({GoalTaskStatus.COMPLETED, GoalTaskStatus.FAILED}),
    GoalTaskStatus.PAUSED: frozenset({GoalTaskStatus.PENDING, GoalTaskStatus.CANCELLED}),
    GoalTaskStatus.COMPLETED: frozenset(), GoalTaskStatus.FAILED: frozenset(),
    GoalTaskStatus.CANCELLED: frozenset(),
}


class PlanManager:
    """Persist a goal task DAG without executing any task."""

    def __init__(self, database: DatabaseManager) -> None:
        self.database = database

    def create_task(self, goal_id: str, title: str, *, dependencies: Iterable[str] = (),
                    **values: Any) -> GoalTask:
        goal_record = self.database.get_goal(goal_id)
        if goal_record is None:
            raise KeyError(goal_id)
        risk = str(values.get("risk", "R1")).upper()
        if int(risk[1:]) > int(str(goal_record["risk_ceiling"])[1:]):
            raise PermissionError(f"task risk {risk} exceeds goal ceiling {goal_record['risk_ceiling']}")
        task = GoalTask(goal_id=goal_id, title=title, dependencies=tuple(dependencies), **values)
        existing = {item.task_id: item for item in self.list_tasks(goal_id)}
        for dependency in task.dependencies:
            if dependency not in existing:
                raise ValueError(f"unknown dependency in goal {goal_id}: {dependency}")
        if task.parent_task_id and task.parent_task_id not in existing:
            raise ValueError(f"unknown parent task in goal {goal_id}: {task.parent_task_id}")
        self._assert_acyclic({**existing, task.task_id: task})
        self.database.save_goal_task(task.to_record(), event_type="task_created", event_payload={})
        return task

    def get_task(self, task_id: str) -> GoalTask:
        record = self.database.get_goal_task(task_id)
        if record is None:
            raise KeyError(task_id)
        return GoalTask.from_record(record)

    def list_tasks(self, goal_id: str) -> list[GoalTask]:
        return [GoalTask.from_record(item) for item in self.database.list_goal_tasks(goal_id)]

    def transition(self, task_id: str, target: GoalTaskStatus | str) -> GoalTask:
        task = self.get_task(task_id)
        normalized = GoalTaskStatus(target)
        if normalized not in _TASK_TRANSITIONS[task.status]:
            raise ValueError(f"invalid goal task transition: {task.status.value} -> {normalized.value}")
        updated = replace(task, status=normalized, updated_at=datetime.now(timezone.utc).isoformat())
        self.database.save_goal_task(
            updated.to_record(), event_type=f"task_{normalized.value}",
            event_payload={"from": task.status.value, "to": normalized.value},
        )
        return updated

    @staticmethod
    def _assert_acyclic(tasks: Mapping[str, GoalTask]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependency graph must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in tasks[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for identifier in tasks:
            visit(identifier)


class PriorityManager:
    _RISK_PENALTY = {"R0": 0, "R1": 2, "R2": 8, "R3": 20, "R4": 40}

    @classmethod
    def score(cls, task: GoalTask, *, goal_priority: int, blocking_count: int = 0) -> int:
        return goal_priority + task.priority + blocking_count * 10 - cls._RISK_PENALTY[task.risk]


class TaskQueue:
    """Expose only dependency-, policy-, and resource-ready tasks."""

    def __init__(self, plans: PlanManager, *, resource_available: Callable[[GoalTask], bool] | None = None) -> None:
        self.plans = plans
        self.resource_available = resource_available or (lambda _task: True)

    def ready(self, goal_id: str) -> list[GoalTask]:
        goal = self.plans.database.get_goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        if GoalStatus(str(goal["status"])) is not GoalStatus.ACTIVE:
            return []
        tasks = self.plans.list_tasks(goal_id)
        by_id = {task.task_id: task for task in tasks}
        candidates: list[tuple[int, GoalTask]] = []
        blocking_counts = {task_id: 0 for task_id in by_id}
        for task in tasks:
            for dependency in task.dependencies:
                blocking_counts[dependency] += 1
        for task in tasks:
            if task.status not in {GoalTaskStatus.PENDING, GoalTaskStatus.READY}:
                continue
            dependencies_ready = all(by_id[item].status is GoalTaskStatus.COMPLETED for item in task.dependencies)
            if not dependencies_ready or task.approval_requirement or not self.resource_available(task):
                continue
            if task.status is GoalTaskStatus.PENDING:
                task = self.plans.transition(task.task_id, GoalTaskStatus.READY)
            score = PriorityManager.score(task, goal_priority=int(goal["priority"]),
                                          blocking_count=blocking_counts[task.task_id])
            candidates.append((score, task))
        return [task for _, task in sorted(candidates, key=lambda item: (-item[0], item[1].created_at, item[1].task_id))]
