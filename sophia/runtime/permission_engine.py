"""Fail-closed permission decisions for registered tool invocations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sophia.runtime.permission import ApprovalCallback, PermissionDenied


class InvocationLike(Protocol):
    spec: object
    supervisor: str
    worker: bool


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str
    user_approval_granted: bool = False
    review_required: bool = False


class PermissionEngine:
    """Apply user-approval and declared-boundary policy before execution.

    Supervisor review is surfaced as a mandatory execution requirement here;
    the Judge/Review stage consumes it before a result is accepted.
    """

    def __init__(self, workspace: str | Path, approval: ApprovalCallback | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.approval = approval

    def authorize(self, invocation: InvocationLike) -> PermissionDecision:
        spec = invocation.spec
        name = str(getattr(spec, "name"))
        operation = str(getattr(spec, "operation"))
        effective_risk = str(getattr(invocation, "effective_risk", getattr(spec, "risk")))
        needs_approval = bool(getattr(spec, "requires_user_approval")) or effective_risk in {"R3", "R4"}
        description = f"{name}: {operation}"
        approved = False
        if needs_approval:
            approved = bool(self.approval and self.approval(description))
            if not approved:
                decision = PermissionDecision(False, "explicit user approval required")
                self._audit(invocation, decision)
                raise PermissionDenied(f"user approval required: {description}")
        decision = PermissionDecision(
            True,
            "approved by registered capability policy",
            user_approval_granted=approved,
            review_required=bool(getattr(spec, "requires_review")),
        )
        self._audit(invocation, decision)
        return decision

    def _audit(self, invocation: InvocationLike, decision: PermissionDecision) -> None:
        spec = invocation.spec
        path = self.workspace / ".sophia" / "permission_audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "tool": getattr(spec, "name"), "risk": getattr(invocation, "effective_risk", getattr(spec, "risk")),
            "supervisor": invocation.supervisor, "worker": invocation.worker,
            "allowed": decision.allowed, "reason": decision.reason,
            "user_approval_granted": decision.user_approval_granted,
            "review_required": decision.review_required,
            "network_access": getattr(spec, "network_access"),
            "secret_access": getattr(spec, "secret_access"),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
