"""Deterministic adapters used by Supervisor dry-run integration tests."""
from __future__ import annotations

from collections import deque
from typing import Any, Mapping

class FakeAdapter:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self._responses = deque(dict(response) for response in responses)
        self.calls: list[dict[str, Any]] = []

    def execute(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(task))
        if not self._responses:
            raise RuntimeError("fake adapter has no response for this step")
        return self._responses.popleft()