from __future__ import annotations

import time
from collections.abc import Callable


class CooldownTracker:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._last_time: dict[str, float] = {}
        self._last_turn: dict[str, int] = {}

    def mark(self, node_id: str, *, turn: int = 0) -> None:
        self._last_time[node_id], self._last_turn[node_id] = self.clock(), turn

    def penalty(self, node_id: str, *, refractory_ms: int = 0, turn: int = 0,
                cooldown_turns: int = 0) -> float:
        if refractory_ms and node_id in self._last_time:
            elapsed_ms = (self.clock() - self._last_time[node_id]) * 1_000
            if elapsed_ms < refractory_ms:
                return 1.0 - max(0.0, elapsed_ms / refractory_ms)
        if cooldown_turns and node_id in self._last_turn:
            remaining = cooldown_turns - (turn - self._last_turn[node_id])
            if remaining > 0:
                return min(1.0, remaining / cooldown_turns)
        return 0.0
