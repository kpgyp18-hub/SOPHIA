"""Two-stage safety escalation with fail-closed decision semantics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sophia.safety.classifier import SafetyAssessment, SafetyVerdict


class SafetyClassifier(Protocol):
    def classify(self, message: str, **kwargs: object) -> SafetyAssessment: ...


@dataclass(frozen=True)
class SafetyDecision:
    """Safety signals only; policy/permission layers retain final authority."""

    assessment: SafetyAssessment
    fast: SafetyAssessment
    deep: SafetyAssessment | None = None
    escalation_reason: str | None = None

    @property
    def allowed(self) -> bool:
        """Only an explicit SAFE result can continue to the permission layer."""
        return self.assessment.verdict is SafetyVerdict.SAFE


class SafetyManager:
    """Run Llama Guard first and cold-load Granite only when policy requires it."""

    _RISK_LEVELS = frozenset({"R0", "R1", "R2", "R3", "R4"})

    def __init__(self, fast: SafetyClassifier, deep: SafetyClassifier,
                 *, deep_risks: frozenset[str] = frozenset({"R2", "R3", "R4"})) -> None:
        invalid = set(deep_risks) - self._RISK_LEVELS
        if invalid:
            raise ValueError(f"unsupported risk levels: {sorted(invalid)}")
        self.fast, self.deep, self.deep_risks = fast, deep, deep_risks

    def assess(self, message: str, *, risk: str = "R0") -> SafetyDecision:
        if risk not in self._RISK_LEVELS:
            raise ValueError(f"unsupported risk level: {risk}")
        fast = self.fast.classify(message)
        reason = None
        if risk in self.deep_risks:
            reason = f"{risk} policy requires deep safety"
        elif fast.requires_escalation:
            reason = f"fast safety returned {fast.verdict.value}"
        if reason is None:
            return SafetyDecision(fast, fast)
        deep = self.deep.classify(message)
        return SafetyDecision(deep, fast, deep, reason)
