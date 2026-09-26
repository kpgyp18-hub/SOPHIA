from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar
from uuid import uuid4

from sophia.database import DatabaseManager
from sophia.goals.manager import GoalManager
from sophia.goals.models import AutonomyBudget
from sophia.goals.planning import GoalTaskStatus, PlanManager
from sophia.runtime.tool_registry import ToolRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    goal_id: str
    owner: str
    expires_at: str
    lease_id: str = field(default_factory=lambda: f"lease-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=lambda: _utc_now().isoformat())
    updated_at: str = field(default_factory=lambda: _utc_now().isoformat())
    status: str = "active"

    def to_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


class TaskLeaseManager:
    KIND = "task_lease"

    def __init__(self, database: DatabaseManager, plans: PlanManager,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        self.database, self.plans, self.clock = database, plans, clock

    def acquire(self, task_id: str, owner: str, *, ttl_seconds: int = 60) -> TaskLease:
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")
        task = self.plans.get_task(task_id)
        current = self.active(task_id)
        if current is not None and current.owner != owner:
            raise RuntimeError(f"task is leased by {current.owner}")
        if current is not None:
            return self.renew(current.lease_id, owner=owner, ttl_seconds=ttl_seconds)
        now = self.clock()
        lease = TaskLease(task_id=task_id, goal_id=task.goal_id, owner=owner,
                          expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
                          created_at=now.isoformat(), updated_at=now.isoformat())
        self._save(lease)
        return lease

    def active(self, task_id: str) -> TaskLease | None:
        records = self.database.list_goal_runtime_records(self.KIND, task_id=task_id, status="active")
        if not records:
            return None
        value = max(records, key=lambda item: str(item["updated_at"]))
        lease = TaskLease(**value)
        if _parse(lease.expires_at) <= self.clock():
            self._save(TaskLease(**{**lease.__dict__, "status": "expired",
                                    "updated_at": self.clock().isoformat()}))
            return None
        return lease

    def renew(self, lease_id: str, *, owner: str, ttl_seconds: int = 60) -> TaskLease:
        value = self.database.get_goal_runtime_record(self.KIND, lease_id)
        if value is None:
            raise KeyError(lease_id)
        lease = TaskLease(**value)
        if lease.owner != owner or lease.status != "active" or _parse(lease.expires_at) <= self.clock():
            raise PermissionError("lease cannot be renewed by this owner")
        now = self.clock()
        updated = TaskLease(**{**lease.__dict__,
                               "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                               "updated_at": now.isoformat()})
        self._save(updated)
        return updated

    def release(self, lease_id: str, *, owner: str) -> None:
        value = self.database.get_goal_runtime_record(self.KIND, lease_id)
        if value is None:
            return
        lease = TaskLease(**value)
        if lease.owner != owner:
            raise PermissionError("lease cannot be released by this owner")
        self._save(TaskLease(**{**lease.__dict__, "status": "released",
                                "updated_at": self.clock().isoformat()}))

    def _save(self, lease: TaskLease) -> None:
        self.database.save_goal_runtime_record(
            self.KIND, lease.lease_id, lease.to_record(), goal_id=lease.goal_id,
            task_id=lease.task_id, status=lease.status,
        )


class HeartbeatManager:
    KIND = "heartbeat"

    def __init__(self, database: DatabaseManager, plans: PlanManager,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        self.database, self.plans, self.clock = database, plans, clock

    def beat(self, task_id: str, *, progress_marker: str, current_step: str) -> dict[str, Any]:
        task = self.plans.get_task(task_id)
        record = {
            "heartbeat_id": f"heartbeat-{task_id}", "task_id": task_id, "goal_id": task.goal_id,
            "progress_marker": progress_marker, "current_step": current_step,
            "created_at": self.clock().isoformat(), "updated_at": self.clock().isoformat(),
        }
        self.database.save_goal_runtime_record(
            self.KIND, record["heartbeat_id"], record, goal_id=task.goal_id,
            task_id=task_id, status="current",
        )
        return record

    def latest(self, task_id: str) -> dict[str, Any] | None:
        return self.database.get_goal_runtime_record(self.KIND, f"heartbeat-{task_id}")


class Watchdog:
    def __init__(self, plans: PlanManager, heartbeats: HeartbeatManager,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        self.plans, self.heartbeats, self.clock = plans, heartbeats, clock

    def inspect(self, goal_id: str, *, timeout_seconds: int) -> list[str]:
        stale: list[str] = []
        for task in self.plans.list_tasks(goal_id):
            if task.status is not GoalTaskStatus.RUNNING:
                continue
            heartbeat = self.heartbeats.latest(task.task_id)
            timestamp = heartbeat.get("updated_at") if heartbeat else task.updated_at
            if (self.clock() - _parse(str(timestamp))).total_seconds() < timeout_seconds:
                continue
            self.plans.transition(task.task_id, GoalTaskStatus.PAUSED)
            stale.append(task.task_id)
        return stale


class AutonomyBudgetTracker:
    KIND = "autonomy_usage"

    def __init__(self, database: DatabaseManager, goals: GoalManager) -> None:
        self.database, self.goals = database, goals

    def usage(self, goal_id: str) -> dict[str, int | float]:
        value = self.database.get_goal_runtime_record(self.KIND, goal_id)
        return dict(value or {"goal_id": goal_id, "runtime_minutes": 0.0, "tool_calls": 0,
                              "r2_actions": 0, "external_calls": 0, "cost": 0.0})

    def consume(self, goal_id: str, *, runtime_minutes: float = 0.0, tool_calls: int = 0,
                r2_actions: int = 0, external_calls: int = 0, cost: float = 0.0) -> dict[str, int | float]:
        current = self.usage(goal_id)
        updated: dict[str, int | float] = {
            "goal_id": goal_id,
            "runtime_minutes": float(current["runtime_minutes"]) + runtime_minutes,
            "tool_calls": int(current["tool_calls"]) + tool_calls,
            "r2_actions": int(current["r2_actions"]) + r2_actions,
            "external_calls": int(current["external_calls"]) + external_calls,
            "cost": float(current["cost"]) + cost,
        }
        budget: AutonomyBudget = self.goals.get(goal_id).autonomy_budget
        checks = {
            "runtime_minutes": budget.max_runtime_minutes, "tool_calls": budget.max_tool_calls,
            "r2_actions": budget.max_r2_actions, "external_calls": budget.max_external_calls,
            "cost": budget.max_cost,
        }
        exceeded = [name for name, maximum in checks.items() if float(updated[name]) > float(maximum)]
        if exceeded:
            raise RuntimeError(f"autonomy budget exceeded: {', '.join(exceeded)}")
        now = _utc_now().isoformat()
        updated["created_at"] = str(current.get("created_at", now))
        updated["updated_at"] = now
        self.database.save_goal_runtime_record(self.KIND, goal_id, updated,
                                               goal_id=goal_id, status="active")
        return updated


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"


@dataclass(frozen=True)
class RuntimeHealth:
    name: str
    state: HealthState
    detail: str
    checked_at: str = field(default_factory=lambda: _utc_now().isoformat())


class BackgroundHealthMonitor:
    def __init__(self) -> None:
        self._probes: dict[str, Callable[[], RuntimeHealth]] = {}

    def register(self, name: str, probe: Callable[[], RuntimeHealth]) -> None:
        if name in self._probes:
            raise ValueError(f"health probe already registered: {name}")
        self._probes[name] = probe

    def check(self) -> list[RuntimeHealth]:
        results: list[RuntimeHealth] = []
        for name, probe in self._probes.items():
            try:
                result = probe()
            except Exception as error:  # probes must degrade, never crash the monitor
                result = RuntimeHealth(name, HealthState.OFFLINE, str(error))
            results.append(result)
        return results


class ArtifactState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    FINAL = "final"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class ArtifactTracker:
    KIND = "artifact"

    def __init__(self, database: DatabaseManager, workspace: str | Path) -> None:
        self.database, self.workspace = database, Path(workspace).resolve()

    def register(self, goal_id: str, task_id: str, path: str | Path, *, artifact_type: str,
                 version: str, state: ArtifactState = ArtifactState.DRAFT) -> dict[str, Any]:
        target = (self.workspace / path).resolve()
        if target != self.workspace and self.workspace not in target.parents:
            raise PermissionError("artifact path escapes workspace")
        if not target.is_file():
            raise FileNotFoundError(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        now = _utc_now().isoformat()
        record = {
            "artifact_id": f"artifact-{uuid4().hex[:12]}", "goal_id": goal_id,
            "task_id": task_id, "path": target.relative_to(self.workspace).as_posix(),
            "type": artifact_type, "version": version, "hash": digest,
            "status": state.value, "created_at": now, "updated_at": now,
        }
        self.database.save_goal_runtime_record(
            self.KIND, record["artifact_id"], record, goal_id=goal_id, task_id=task_id,
            status=state.value,
        )
        return record


class FailureBudgetManager:
    KIND = "failure_budget"

    def __init__(self, database: DatabaseManager, goals: GoalManager) -> None:
        self.database, self.goals = database, goals

    def record(self, goal_id: str, failure_type: str, *, limits: Mapping[str, int]) -> dict[str, Any]:
        current = self.database.get_goal_runtime_record(self.KIND, goal_id) or {
            "goal_id": goal_id, "counts": {}, "created_at": _utc_now().isoformat(),
        }
        counts = dict(current.get("counts", {}))
        counts[failure_type] = int(counts.get(failure_type, 0)) + 1
        exceeded = counts[failure_type] > int(limits.get(failure_type, 0))
        updated = {**current, "counts": counts, "degraded": exceeded,
                   "updated_at": _utc_now().isoformat()}
        self.database.save_goal_runtime_record(self.KIND, goal_id, updated,
                                               goal_id=goal_id,
                                               status="degraded" if exceeded else "active")
        if exceeded and self.goals.get(goal_id).status.value == "active":
            self.goals.pause(goal_id)
        return updated


_Result = TypeVar("_Result")


class BackgroundRuntime:
    """Bounded R0/R1 execution through ToolRegistry with leases and heartbeats."""

    def __init__(self, plans: PlanManager, tools: ToolRegistry, leases: TaskLeaseManager,
                 heartbeats: HeartbeatManager, budgets: AutonomyBudgetTracker) -> None:
        self.plans, self.tools, self.leases = plans, tools, leases
        self.heartbeats, self.budgets = heartbeats, budgets

    def run_once(self, task_id: str, *, worker_id: str, tool_name: str,
                 handler: Callable[..., _Result], request_context: str = "background maintenance",
                 idempotency_key: str | None = None) -> _Result:
        task = self.plans.get_task(task_id)
        spec = self.tools.get(tool_name)
        if task.status is not GoalTaskStatus.READY:
            raise ValueError("background task must be READY")
        if task.risk not in {"R0", "R1"} or spec.risk not in {"R0", "R1"}:
            raise PermissionError("background execution is limited to R0/R1")
        if spec.network_access or spec.secret_access:
            raise PermissionError("background execution cannot access network or secrets")
        if task.approval_requirement or spec.requires_user_approval:
            raise PermissionError("background execution cannot satisfy approval requirements")
        lease = self.leases.acquire(task_id, worker_id)
        started = time.monotonic()
        self.budgets.consume(task.goal_id, tool_calls=1)
        self.plans.transition(task_id, GoalTaskStatus.RUNNING)
        self.heartbeats.beat(task_id, progress_marker="started", current_step=tool_name)
        try:
            result = self.tools.dispatch(
                tool_name, "aide", handler, worker=True, request_context=request_context,
                task_id=task_id, idempotency_key=idempotency_key,
            )
        except Exception:
            self.plans.transition(task_id, GoalTaskStatus.FAILED)
            raise
        else:
            self.heartbeats.beat(task_id, progress_marker="completed", current_step=tool_name)
            self.plans.transition(task_id, GoalTaskStatus.COMPLETED)
            return result
        finally:
            elapsed = (time.monotonic() - started) / 60
            if elapsed:
                self.budgets.consume(task.goal_id, runtime_minutes=elapsed)
            self.leases.release(lease.lease_id, owner=worker_id)
