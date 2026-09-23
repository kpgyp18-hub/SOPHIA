"""Adapter boundary for Supervisor-managed agent execution."""
from __future__ import annotations

from typing import Protocol, Mapping, Any

class AgentAdapter(Protocol):
    def execute(self, task: Mapping[str, Any]) -> dict[str, Any]:
        """Return a machine-protocol out payload without invoking peer CLIs."""