"""Cooperative cancellation tokens shared by Core, workers, and tools."""
from __future__ import annotations

from dataclasses import dataclass


class TaskCancelledError(RuntimeError):
    pass


@dataclass
class CancellationToken:
    task_id: str
    requested: bool = False

    def cancel(self) -> None:
        self.requested = True

    def raise_if_cancelled(self) -> None:
        if self.requested:
            raise TaskCancelledError(f"CANCELLED: task {self.task_id}")


class CancellationRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}

    def create(self, task_id: str) -> CancellationToken:
        token = CancellationToken(task_id)
        self._tokens[task_id] = token
        return token

    def request(self, task_id: str) -> bool:
        token = self._tokens.get(task_id)
        if token is None:
            return False
        token.cancel()
        return True

    def get(self, task_id: str) -> CancellationToken | None:
        return self._tokens.get(task_id)

    def clear(self, task_id: str) -> None:
        self._tokens.pop(task_id, None)
