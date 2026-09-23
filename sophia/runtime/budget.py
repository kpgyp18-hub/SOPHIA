"""Explicit context budget accounting (v0.2)."""
from __future__ import annotations


class ContextBudget:
    def __init__(self, token_limit: int = 8_192) -> None:
        self.token_limit = token_limit

    @staticmethod
    def estimate(text: str) -> int:
        # Stable, dependency-free approximation suitable for routing telemetry.
        return max(1, (len(text) + 3) // 4) if text else 0

    def fit(self, text: str) -> str:
        max_chars = self.token_limit * 4
        return text if len(text) <= max_chars else text[-max_chars:]
