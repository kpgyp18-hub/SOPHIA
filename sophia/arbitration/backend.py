from __future__ import annotations

from typing import Protocol

from sophia.arbitration.cooldown import CooldownTracker
from sophia.arbitration.graph import ActivationGraph, ArbitrationResult
from sophia.arbitration.modes import ArbitrationMode


class ArbitrationBackend(Protocol):
    def evaluate(self, features: dict[str, float], *, state: dict[str, float], mode: ArbitrationMode,
                 cooldown: CooldownTracker, turn: int) -> ArbitrationResult: ...


class DeterministicBackend:
    def __init__(self, graph: ActivationGraph) -> None:
        self.graph = graph

    def evaluate(self, features: dict[str, float], *, state: dict[str, float], mode: ArbitrationMode,
                 cooldown: CooldownTracker, turn: int) -> ArbitrationResult:
        return self.graph.evaluate(features, state=state, mode=mode, cooldown=cooldown, turn=turn)
