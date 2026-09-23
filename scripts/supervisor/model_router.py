"""Capability-only deterministic model tier selection."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from .feature_builder import semantic_features_missing

@dataclass(frozen=True)
class ModelDecision:
    status: str
    model_tier: str | None
    reason: str | None = None

class CodexModelTierRouter:
    def __init__(self, escalation_threshold: int = 2) -> None:
        self.escalation_threshold = escalation_threshold

    def decide(self, features: Mapping[str, Any]) -> ModelDecision:
        if semantic_features_missing(features):
            return ModelDecision("NEED_ARBITER", None, "required semantic features are missing")
        complexity = features.get("complexity")
        reasoning = features.get("requires_reasoning")
        failures = features.get("same_failure_count", 0)
        if complexity == "simple" and features.get("file_count", 0) <= 2 and reasoning is False:
            return ModelDecision("READY", "LUNA")
        if complexity == "normal" and reasoning is True and failures < self.escalation_threshold:
            return ModelDecision("READY", "TERRA")
        if complexity == "complex" or failures >= self.escalation_threshold:
            return ModelDecision("READY", "SOL")
        return ModelDecision("NEED_ARBITER", None, "features do not satisfy a deterministic rule")


# Backward-compatible name. SophiaCore owns task routing; this class only
# selects the Codex execution model tier from already-classified features.
ModelRouter = CodexModelTierRouter
