"""Deterministic retrieval destination classification for v0.8."""
from __future__ import annotations

from enum import StrEnum


class RetrievalTarget(StrEnum):
    MEMORY = "memory"
    SKILL = "skill"
    DECISION_TRACE = "decision_trace"


class RetrievalRouter:
    _SKILL_TERMS = ("skill", "procedure", "workflow", "스킬", "절차", "워크플로")
    _DECISION_TERMS = ("decision", "why", "reason", "trace", "결정", "근거", "왜")

    def classify(self, query: str) -> RetrievalTarget:
        lowered = query.casefold()
        if any(term in lowered for term in self._DECISION_TERMS):
            return RetrievalTarget.DECISION_TRACE
        if any(term in lowered for term in self._SKILL_TERMS):
            return RetrievalTarget.SKILL
        return RetrievalTarget.MEMORY
