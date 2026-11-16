"""G2 common attempt identity and monotonic hard-deadline contract.

The current entrypoints retain their owners. A caller may pass this contract
through retries or handoffs; creating a child attempt never refreshes its
original deadline. No provider or process is started by this module.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .execution_boundaries import BOUNDARIES


@dataclass(frozen=True)
class ExecutionContract:
    surface: str
    task_id: str
    attempt_id: str
    started_at: float
    deadline_at: float
    version: int = 1

    def __post_init__(self) -> None:
        if self.surface not in BOUNDARIES:
            raise ValueError("unknown execution surface")
        if not self.task_id.strip() or not self.attempt_id.strip():
            raise ValueError("task_id and attempt_id are required")
        if self.version != 1:
            raise ValueError("unsupported execution contract version")
        if not (math.isfinite(self.started_at) and math.isfinite(self.deadline_at)):
            raise ValueError("deadline must be finite")
        if self.deadline_at <= self.started_at:
            raise ValueError("deadline must follow start")

    @classmethod
    def start(cls, surface: str, task_id: str, attempt_id: str, timeout_s: float,
              *, now: float | None = None) -> "ExecutionContract":
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout must be positive and finite")
        start = time.monotonic() if now is None else now
        return cls(surface, task_id, attempt_id, start, start + timeout_s)

    def remaining_s(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        if not math.isfinite(current):
            raise ValueError("current time must be finite")
        return max(0.0, self.deadline_at - current)

    def require_remaining_s(self, *, now: float | None = None) -> float:
        remaining = self.remaining_s(now=now)
        if remaining <= 0:
            raise TimeoutError("execution deadline exhausted before new attempt")
        return remaining

    def next_attempt(self, attempt_id: str) -> "ExecutionContract":
        """Keep task identity and the original deadline across a retry."""
        if attempt_id == self.attempt_id:
            raise ValueError("retry requires a new attempt_id")
        return ExecutionContract(self.surface, self.task_id, attempt_id,
                                 self.started_at, self.deadline_at, self.version)
