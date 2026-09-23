"""Shared, fail-closed contracts for local safety classifiers."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SafetyVerdict(StrEnum):
    SAFE = "safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SafetyAssessment:
    """A model's non-authoritative safety signal.

    ``UNKNOWN`` covers model failures and malformed answers.  Policy code must
    never treat it as permission to execute an action.
    """

    verdict: SafetyVerdict
    categories: tuple[str, ...] = ()
    raw: str = ""
    model: str | None = None
    detail: str = ""

    @property
    def requires_escalation(self) -> bool:
        return self.verdict is not SafetyVerdict.SAFE
