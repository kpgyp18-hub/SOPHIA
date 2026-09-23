from __future__ import annotations

from sophia.decisions.base import DecisionOutcome, DecisionProvider


class DecisionManager:
    """Qwen Fast primary with deterministic Rules fallback and explicit confidence policy."""

    def __init__(self, primary: DecisionProvider | None, fallback: DecisionProvider,
                 *, auto_threshold: float = 0.90, review_threshold: float = 0.70,
                 crosscheck_threshold: float = 0.50) -> None:
        self.primary, self.fallback = primary, fallback
        self.auto_threshold = auto_threshold
        self.review_threshold = review_threshold
        self.crosscheck_threshold = crosscheck_threshold

    def decide(self, user_input: str) -> DecisionOutcome:
        if self.primary is None:
            return self.fallback.decide(user_input)
        try:
            outcome = self.primary.decide(user_input)
        except (OSError, TimeoutError, ValueError, KeyError, TypeError):
            return self.fallback.decide(user_input)
        if outcome.confidence >= self.auto_threshold:
            return outcome
        if outcome.confidence >= self.review_threshold:
            return DecisionOutcome(**{**outcome.__dict__, "review_required": True})
        if outcome.confidence >= self.crosscheck_threshold:
            rule = self.fallback.decide(user_input)
            if rule.task_type != outcome.task_type:
                return rule
            return DecisionOutcome(**{**outcome.__dict__, "crosscheck_required": True})
        return self.fallback.decide(user_input)
