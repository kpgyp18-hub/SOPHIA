"""Deterministic escalation planning for S.O.P.H.I.A tasks.

Escalation is a recorded recommendation, never an automatic external-model
call.  Moving work outside the local environment must remain user-approved.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.supervisor.model_router import ModelRouter  # noqa: E402

from sophia.runtime.task import Task, TaskType


class EscalationRoute(StrEnum):
    LOCAL = "local"
    DUAL_REVIEW = "dual_review"
    USER_APPROVAL = "user_approval"


def _complexity_bucket(complexity: float) -> str:
    if complexity < 0.35:
        return "simple"
    if complexity < 0.65:
        return "normal"
    return "complex"


def suggest_codex_tier(task: Task, *, same_failure_count: int = 0) -> str | None:
    """Ask the existing scripts/supervisor ModelRouter which Codex tier (LUNA/TERRA/SOL)
    would handle this task, without making any external call. Returns None when the
    deterministic rules can't decide (NEED_ARBITER) so the caller must ask a human/Blanc.
    """
    router = ModelRouter()
    features = {
        "complexity": _complexity_bucket(task.complexity),
        "requires_reasoning": task.task_type in (TaskType.ARCHITECTURE, TaskType.REVIEW),
        "file_count": 0,
        "same_failure_count": same_failure_count,
    }
    decision = router.decide(features)
    return decision.model_tier if decision.status == "READY" else None


@dataclass(frozen=True)
class EscalationDecision:
    route: EscalationRoute
    reason: str
    next_supervisor: str | None = None
    requires_user_approval: bool = False
    suggested_tier: str | None = None

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "route": self.route,
            "reason": self.reason,
            "next_supervisor": self.next_supervisor,
            "requires_user_approval": self.requires_user_approval,
            "suggested_tier": self.suggested_tier,
        }


class EscalationPolicy:
    """Choose the next safe path without making a side-effecting call."""

    _EXTERNAL_TERMS = ("external", "cloud model", "api call", "외부 모델", "외부모델")

    def plan(self, task: Task) -> EscalationDecision:
        lowered = task.input.lower()
        if any(term in lowered for term in self._EXTERNAL_TERMS):
            return EscalationDecision(
                EscalationRoute.USER_APPROVAL,
                "external model requested",
                requires_user_approval=True,
                suggested_tier=suggest_codex_tier(task),
            )
        if task.task_type in (TaskType.ARCHITECTURE, TaskType.REVIEW) or task.complexity >= 0.5:
            return EscalationDecision(
                EscalationRoute.DUAL_REVIEW,
                "complex planning or review task",
                next_supervisor="aide",
            )
        return EscalationDecision(EscalationRoute.LOCAL, "local execution is sufficient")

    def after_failure(self, task: Task) -> EscalationDecision:
        if task.task_type in (TaskType.CODING, TaskType.DEBUG):
            return EscalationDecision(
                EscalationRoute.DUAL_REVIEW,
                "local execution reported a failure",
                next_supervisor="blanc",
            )
        return EscalationDecision(
            EscalationRoute.USER_APPROVAL,
            "local execution reported a failure and needs direction",
            requires_user_approval=True,
            suggested_tier=suggest_codex_tier(task),
        )
