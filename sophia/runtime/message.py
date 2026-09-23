"""Compact, durable machine messages for supervisor handoffs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentMessage:
    """A bounded internal handoff; persona prose remains outside this contract."""

    sender: str
    receiver: str
    task_id: str
    status: str
    summary: str
    risk: str = "low"
    request: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

