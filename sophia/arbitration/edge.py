from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EdgeKind(StrEnum):
    EXCITATORY = "excitatory"
    INHIBITORY = "inhibitory"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    weight: float
    kind: EdgeKind | str = EdgeKind.EXCITATORY
    learnable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EdgeKind(self.kind))
        if not self.source or not self.target or not -1.0 <= self.weight <= 1.0:
            raise ValueError("edge endpoints and weight in [-1,1] are required")

    @property
    def signed_weight(self) -> float:
        if self.kind is EdgeKind.NEUTRAL:
            return 0.0
        return abs(self.weight) if self.kind is EdgeKind.EXCITATORY else -abs(self.weight)
