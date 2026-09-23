"""Bounded in-memory call graph for observable supervisor/provider routing."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class CallNode:
    call_id: str
    task_id: str
    supervisor: str
    provider: str
    brain: str
    parent_call_id: str | None = None
    status: str = "active"
    started_at: str = ""
    finished_at: str | None = None


class CallGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, CallNode] = {}

    def start(self, call_id: str, *, task_id: str, supervisor: str, provider: str,
              brain: str, parent_call_id: str | None = None) -> CallNode:
        node = CallNode(call_id, task_id, supervisor, provider, brain, parent_call_id,
                        started_at=datetime.now(timezone.utc).isoformat())
        self._nodes[call_id] = node
        return node

    def finish(self, call_id: str, *, status: str) -> CallNode:
        node = self._nodes[call_id]
        node.status, node.finished_at = status, datetime.now(timezone.utc).isoformat()
        return node

    def snapshot(self, task_id: str) -> list[dict[str, str | None]]:
        return [asdict(node) for node in self._nodes.values() if node.task_id == task_id]
