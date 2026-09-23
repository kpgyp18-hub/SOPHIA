"""Per-provider failure containment with deterministic state transitions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    pass


@dataclass
class _Circuit:
    failures: int = 0
    state: CircuitState = CircuitState.CLOSED
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0, clock=monotonic) -> None:
        if failure_threshold < 1 or cooldown_seconds < 0:
            raise ValueError("failure_threshold must be positive and cooldown non-negative")
        self.failure_threshold, self.cooldown_seconds, self._clock = failure_threshold, cooldown_seconds, clock
        self._circuits: dict[str, _Circuit] = {}

    def state(self, provider: str) -> CircuitState:
        circuit = self._circuits.setdefault(provider, _Circuit())
        if circuit.state is CircuitState.OPEN and circuit.opened_at is not None and self._clock() - circuit.opened_at >= self.cooldown_seconds:
            circuit.state = CircuitState.HALF_OPEN
        return circuit.state

    def allow(self, provider: str) -> None:
        if self.state(provider) is CircuitState.OPEN:
            raise CircuitOpenError(f"CIRCUIT_OPEN: provider '{provider}' is cooling down")

    def success(self, provider: str) -> None:
        circuit = self._circuits.setdefault(provider, _Circuit())
        circuit.failures, circuit.state, circuit.opened_at = 0, CircuitState.CLOSED, None

    def failure(self, provider: str) -> CircuitState:
        circuit = self._circuits.setdefault(provider, _Circuit())
        circuit.failures += 1
        if circuit.state is CircuitState.HALF_OPEN or circuit.failures >= self.failure_threshold:
            circuit.state, circuit.opened_at = CircuitState.OPEN, self._clock()
        return circuit.state
