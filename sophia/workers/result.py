from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WorkerStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class WorkerResult:
    """A short-lived worker result; only this value returns to the caller."""

    worker_id: str
    call_id: str
    status: WorkerStatus
    value: Any = None
    error: str | None = None
    attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
