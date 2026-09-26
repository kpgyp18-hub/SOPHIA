from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4

from sophia.database import DatabaseManager
from sophia.goals.planning import PlanManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalRequest:
    task_id: str
    goal_id: str
    action: str
    risk: str
    reason: str
    expected_effect: str
    rollback_plan: str
    expires_at: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    approval_id: str = field(default_factory=lambda: f"approval-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    decided_by: str | None = None

    def to_record(self) -> dict[str, Any]:
        result = dict(self.__dict__)
        result["status"] = self.status.value
        return result

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> ApprovalRequest:
        return cls(**{**dict(value), "status": ApprovalStatus(str(value["status"]))})


class ApprovalQueue:
    KIND = "approval"

    def __init__(self, database: DatabaseManager, plans: PlanManager) -> None:
        self.database, self.plans = database, plans

    def request(self, task_id: str, *, action: str, reason: str, expected_effect: str,
                rollback_plan: str, expires_at: str | None = None) -> ApprovalRequest:
        task = self.plans.get_task(task_id)
        approval = ApprovalRequest(
            task_id=task_id, goal_id=task.goal_id, action=action, risk=task.risk,
            reason=reason, expected_effect=expected_effect, rollback_plan=rollback_plan,
            expires_at=expires_at,
        )
        self._save(approval)
        self.database.record("goal.approval_requested", approval.to_record())
        return approval

    def get(self, approval_id: str) -> ApprovalRequest:
        value = self.database.get_goal_runtime_record(self.KIND, approval_id)
        if value is None:
            raise KeyError(approval_id)
        approval = ApprovalRequest.from_record(value)
        if (approval.status is ApprovalStatus.PENDING and approval.expires_at and
                datetime.fromisoformat(approval.expires_at) <= datetime.now(timezone.utc)):
            approval = ApprovalRequest(**{**approval.__dict__, "status": ApprovalStatus.EXPIRED,
                                          "updated_at": _now()})
            self._save(approval)
        return approval

    def list(self, *, goal_id: str | None = None,
             status: ApprovalStatus | str | None = None) -> list[ApprovalRequest]:
        normalized = ApprovalStatus(status).value if status is not None else None
        return [ApprovalRequest.from_record(item) for item in
                self.database.list_goal_runtime_records(self.KIND, goal_id=goal_id, status=normalized)]

    def approve(self, approval_id: str, *, actor: str) -> ApprovalRequest:
        return self._decide(approval_id, ApprovalStatus.APPROVED, actor)

    def reject(self, approval_id: str, *, actor: str) -> ApprovalRequest:
        return self._decide(approval_id, ApprovalStatus.REJECTED, actor)

    def granted(self, task_id: str) -> bool:
        return any(item.status is ApprovalStatus.APPROVED for item in self.list_for_task(task_id))

    def list_for_task(self, task_id: str) -> list[ApprovalRequest]:
        return [ApprovalRequest.from_record(item) for item in
                self.database.list_goal_runtime_records(self.KIND, task_id=task_id)]

    def _decide(self, approval_id: str, status: ApprovalStatus, actor: str) -> ApprovalRequest:
        if actor != "user":
            raise PermissionError("only the user can decide an approval request")
        approval = self.get(approval_id)
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError(f"approval is already {approval.status.value}")
        updated = ApprovalRequest(**{**approval.__dict__, "status": status,
                                     "updated_at": _now(), "decided_by": actor})
        self._save(updated)
        self.database.record(f"goal.approval_{status.value}", updated.to_record())
        return updated

    def _save(self, approval: ApprovalRequest) -> None:
        self.database.save_goal_runtime_record(
            self.KIND, approval.approval_id, approval.to_record(), goal_id=approval.goal_id,
            task_id=approval.task_id, status=approval.status.value,
        )
