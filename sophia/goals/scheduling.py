from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping
from uuid import uuid4

from sophia.database import DatabaseManager
from sophia.goals.planning import GoalTask, GoalTaskStatus, PlanManager, TaskQueue


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TriggerType(StrEnum):
    TIME = "time"
    EVENT = "event"
    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    USER = "user"
    HEALTH = "health"
    FILE_CHANGE = "file_change"
    DB_STATE = "db_state"


@dataclass(frozen=True)
class Schedule:
    task_id: str
    goal_id: str
    trigger_type: TriggerType
    next_run: str
    interval_seconds: int | None = None
    trigger_config: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    schedule_id: str = field(default_factory=lambda: f"schedule-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())

    def __post_init__(self) -> None:
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        _parse(self.next_run)

    def to_record(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id, "task_id": self.task_id, "goal_id": self.goal_id,
            "trigger_type": self.trigger_type.value, "next_run": self.next_run,
            "interval_seconds": self.interval_seconds, "trigger_config": dict(self.trigger_config),
            "enabled": self.enabled, "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> Schedule:
        return cls(
            schedule_id=str(value["schedule_id"]), task_id=str(value["task_id"]),
            goal_id=str(value["goal_id"]), trigger_type=TriggerType(str(value["trigger_type"])),
            next_run=str(value["next_run"]),
            interval_seconds=int(value["interval_seconds"]) if value.get("interval_seconds") else None,
            trigger_config=dict(value.get("trigger_config", {})), enabled=bool(value.get("enabled", True)),
            created_at=str(value["created_at"]), updated_at=str(value["updated_at"]),
        )


class TriggerEngine:
    def matches(self, schedule: Schedule, *, now: datetime, event: str | None = None,
                resource_available: bool = True, health_ok: bool = True,
                dependency_ready: bool = True) -> bool:
        if not schedule.enabled:
            return False
        if schedule.trigger_type is TriggerType.TIME:
            return _parse(schedule.next_run) <= now
        if schedule.trigger_type is TriggerType.EVENT:
            return event == schedule.trigger_config.get("event")
        if schedule.trigger_type is TriggerType.DEPENDENCY:
            return dependency_ready
        if schedule.trigger_type is TriggerType.RESOURCE:
            return resource_available
        if schedule.trigger_type is TriggerType.HEALTH:
            return health_ok
        if schedule.trigger_type is TriggerType.USER:
            return event == "user"
        return bool(event and event == schedule.trigger_config.get("event"))


class Scheduler:
    """Persist wake-up intents. It never executes a task or grants permission."""

    KIND = "schedule"

    def __init__(self, database: DatabaseManager, plans: PlanManager,
                 queue: TaskQueue | None = None, trigger_engine: TriggerEngine | None = None,
                 clock: Callable[[], datetime] = _now) -> None:
        self.database, self.plans = database, plans
        self.queue = queue or TaskQueue(plans)
        self.triggers = trigger_engine or TriggerEngine()
        self.clock = clock

    def add(self, task_id: str, *, trigger_type: TriggerType | str = TriggerType.TIME,
            next_run: str | None = None, interval_seconds: int | None = None,
            trigger_config: Mapping[str, Any] | None = None) -> Schedule:
        task = self.plans.get_task(task_id)
        if task.risk not in {"R0", "R1"} or task.approval_requirement:
            raise PermissionError("background scheduler accepts only approval-free R0/R1 tasks")
        schedule = Schedule(
            task_id=task_id, goal_id=task.goal_id, trigger_type=TriggerType(trigger_type),
            next_run=next_run or self.clock().isoformat(), interval_seconds=interval_seconds,
            trigger_config=dict(trigger_config or {}),
        )
        self._save(schedule)
        self.database.record("goal.schedule_added", schedule.to_record())
        return schedule

    def get(self, schedule_id: str) -> Schedule:
        value = self.database.get_goal_runtime_record(self.KIND, schedule_id)
        if value is None:
            raise KeyError(schedule_id)
        return Schedule.from_record(value)

    def list(self, *, goal_id: str | None = None, enabled_only: bool = False) -> list[Schedule]:
        schedules = [Schedule.from_record(item) for item in
                     self.database.list_goal_runtime_records(self.KIND, goal_id=goal_id)]
        return [item for item in schedules if item.enabled] if enabled_only else schedules

    def pause(self, schedule_id: str) -> Schedule:
        schedule = self.get(schedule_id)
        updated = Schedule(**{**schedule.__dict__, "enabled": False, "updated_at": self.clock().isoformat()})
        self._save(updated)
        return updated

    def due(self, *, event: str | None = None, goal_id: str | None = None,
            resource_available: bool = True, health_ok: bool = True) -> list[GoalTask]:
        now = self.clock()
        ready: list[GoalTask] = []
        for schedule in self.list(goal_id=goal_id, enabled_only=True):
            task = self.plans.get_task(schedule.task_id)
            dependencies_ready = all(
                self.plans.get_task(identifier).status is GoalTaskStatus.COMPLETED
                for identifier in task.dependencies
            )
            if not self.triggers.matches(schedule, now=now, event=event,
                                         resource_available=resource_available,
                                         health_ok=health_ok,
                                         dependency_ready=dependencies_ready):
                continue
            if task.status is GoalTaskStatus.COMPLETED and schedule.interval_seconds is not None:
                task = self.plans.create_task(
                    task.goal_id, task.title, description=task.description,
                    parent_task_id=task.parent_task_id, priority=task.priority,
                    dependencies=task.dependencies, required_skills=task.required_skills,
                    required_capabilities=task.required_capabilities, risk=task.risk,
                    approval_requirement=task.approval_requirement, deadline=task.deadline,
                    retry_policy=task.retry_policy, checkpoint_policy=task.checkpoint_policy,
                )
                schedule = Schedule(**{**schedule.__dict__, "task_id": task.task_id,
                                       "updated_at": now.isoformat()})
            if task.status is GoalTaskStatus.PAUSED:
                task = self.plans.transition(task.task_id, GoalTaskStatus.PENDING)
            if task.status is GoalTaskStatus.WAITING:
                task = self.plans.transition(task.task_id, GoalTaskStatus.PENDING)
            # Queue performs the final dependency/resource/approval gate.
            candidates = {item.task_id: item for item in self.queue.ready(task.goal_id)}
            if task.task_id in candidates:
                ready.append(candidates[task.task_id])
            if schedule.interval_seconds is None:
                updated = Schedule(**{**schedule.__dict__, "enabled": False,
                                      "updated_at": now.isoformat()})
            else:
                updated = Schedule(**{**schedule.__dict__,
                    "next_run": (now + timedelta(seconds=schedule.interval_seconds)).isoformat(),
                    "updated_at": now.isoformat()})
            self._save(updated)
        return ready

    def _save(self, schedule: Schedule) -> None:
        self.database.save_goal_runtime_record(
            self.KIND, schedule.schedule_id, schedule.to_record(), goal_id=schedule.goal_id,
            task_id=schedule.task_id, status="enabled" if schedule.enabled else "paused",
        )


class DeadlineManager:
    def __init__(self, plans: PlanManager, *, clock: Callable[[], datetime] = _now) -> None:
        self.plans, self.clock = plans, clock

    def alerts(self, goal_id: str, *, warning_seconds: int = 3600) -> list[dict[str, str]]:
        now = self.clock()
        alerts: list[dict[str, str]] = []
        for task in self.plans.list_tasks(goal_id):
            if not task.deadline or task.status in {GoalTaskStatus.COMPLETED, GoalTaskStatus.CANCELLED}:
                continue
            remaining = (_parse(task.deadline) - now).total_seconds()
            if remaining <= warning_seconds:
                alerts.append({"type": "USER_ALERT", "task_id": task.task_id,
                               "state": "overdue" if remaining < 0 else "at_risk"})
        return alerts
