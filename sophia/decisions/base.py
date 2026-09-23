from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DecisionOutcome:
    task_type: str
    complexity: float
    confidence: float
    provider: str
    reason: str
    review_required: bool = False
    crosscheck_required: bool = False


class DecisionProvider(Protocol):
    def decide(self, user_input: str) -> DecisionOutcome: ...
