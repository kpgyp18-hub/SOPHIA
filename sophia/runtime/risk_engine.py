"""Deterministic request-sensitive risk escalation for registered tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


_RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}
_DESTRUCTIVE_MARKERS = ("delete", "remove", "drop table", "drop database", "truncate", "reset --hard", "format", "shutdown", "reboot")
_EXTERNAL_OR_SECRET_MARKERS = (
    "api key", "apikey", "token", "password", "credential", "secret", "upload", "public push", "git push",
)


class InvocationLike(Protocol):
    spec: object


@dataclass(frozen=True)
class RiskDecision:
    risk: str
    reason: str


class RiskEngine:
    """Never lower registry risk; elevate it when request content is riskier."""

    def assess(self, invocation: InvocationLike, request_context: str = "") -> RiskDecision:
        declared = str(getattr(invocation.spec, "risk"))
        if declared not in _RISK_ORDER:
            raise ValueError(f"unsupported risk level: {declared}")
        text = request_context.lower()
        inferred = "R0"
        reason = "registered tool metadata"
        if any(marker in text for marker in _EXTERNAL_OR_SECRET_MARKERS):
            inferred, reason = "R4", "external or secret-bearing request content"
        elif any(marker in text for marker in _DESTRUCTIVE_MARKERS):
            inferred, reason = "R3", "destructive request content"
        effective = inferred if _RISK_ORDER[inferred] > _RISK_ORDER[declared] else declared
        return RiskDecision(effective, reason if effective == inferred else "registered tool metadata")
