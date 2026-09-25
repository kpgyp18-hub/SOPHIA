from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping
from uuid import uuid4


class GoalStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AutonomyBudget:
    max_runtime_minutes: int = 120
    max_tool_calls: int = 100
    max_r2_actions: int = 20
    max_external_calls: int = 0
    max_cost: float = 0.0

    def __post_init__(self) -> None:
        for name in ("max_runtime_minutes", "max_tool_calls", "max_r2_actions", "max_external_calls"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.max_cost < 0:
            raise ValueError("max_cost cannot be negative")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_runtime_minutes": self.max_runtime_minutes,
            "max_tool_calls": self.max_tool_calls,
            "max_r2_actions": self.max_r2_actions,
            "max_external_calls": self.max_external_calls,
            "max_cost": self.max_cost,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> AutonomyBudget:
        return cls(**dict(value or {}))


def _unique_text(values: Iterable[str], *, field_name: str, required: bool = False) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if required and not result:
        raise ValueError(f"{field_name} must contain at least one non-empty value")
    return result


@dataclass(frozen=True)
class Goal:
    title: str
    success_criteria: tuple[str, ...]
    description: str = ""
    owner: str = "user"
    target_date: str | None = None
    priority: int = 0
    status: GoalStatus = GoalStatus.DRAFT
    constraints: tuple[str, ...] = ()
    risk_ceiling: str = "R1"
    autonomy_budget: AutonomyBudget = field(default_factory=AutonomyBudget)
    satisfied_criteria: tuple[str, ...] = ()
    goal_id: str = field(default_factory=lambda: f"goal-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        title = self.title.strip()
        if not title:
            raise ValueError("goal title is required")
        criteria = _unique_text(self.success_criteria, field_name="success_criteria", required=True)
        satisfied = _unique_text(self.satisfied_criteria, field_name="satisfied_criteria")
        unknown = set(satisfied) - set(criteria)
        if unknown:
            raise ValueError(f"satisfied criteria are not declared: {sorted(unknown)}")
        risk = self.risk_ceiling.upper()
        if risk not in {"R0", "R1", "R2", "R3", "R4"}:
            raise ValueError(f"unsupported risk ceiling: {self.risk_ceiling}")
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "owner", self.owner.strip() or "user")
        object.__setattr__(self, "success_criteria", criteria)
        object.__setattr__(self, "constraints", _unique_text(self.constraints, field_name="constraints"))
        object.__setattr__(self, "satisfied_criteria", satisfied)
        object.__setattr__(self, "risk_ceiling", risk)

    @property
    def progress(self) -> float:
        return len(self.satisfied_criteria) / len(self.success_criteria)

    @property
    def completion_ready(self) -> bool:
        return set(self.satisfied_criteria) == set(self.success_criteria)

    def to_record(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "title": self.title,
            "description": self.description,
            "owner": self.owner,
            "target_date": self.target_date,
            "priority": self.priority,
            "status": self.status.value,
            "success_criteria": list(self.success_criteria),
            "satisfied_criteria": list(self.satisfied_criteria),
            "constraints": list(self.constraints),
            "risk_ceiling": self.risk_ceiling,
            "autonomy_budget": self.autonomy_budget.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> Goal:
        return cls(
            goal_id=str(value["goal_id"]),
            title=str(value["title"]),
            description=str(value.get("description", "")),
            owner=str(value.get("owner", "user")),
            target_date=str(value["target_date"]) if value.get("target_date") is not None else None,
            priority=int(value.get("priority", 0)),
            status=GoalStatus(str(value.get("status", GoalStatus.DRAFT.value))),
            success_criteria=tuple(value.get("success_criteria", ())),
            satisfied_criteria=tuple(value.get("satisfied_criteria", ())),
            constraints=tuple(value.get("constraints", ())),
            risk_ceiling=str(value.get("risk_ceiling", "R1")),
            autonomy_budget=AutonomyBudget.from_mapping(value.get("autonomy_budget")),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )
