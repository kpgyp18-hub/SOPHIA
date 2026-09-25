from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class NodeType(StrEnum):
    INPUT = "input"
    STATE = "state"
    SUPERVISOR = "supervisor"
    SKILL = "skill"
    SAFETY = "safety"
    MEMORY = "memory"
    TOOL_NEED = "tool_need"
    ACTION = "action"
    MODE = "mode"
    PROVIDER = "provider"


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: NodeType | str
    threshold: float = 0.5
    bias: float = 0.0
    refractory_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_type", NodeType(self.node_type))
        if not self.node_id or not 0.0 <= self.threshold <= 1.0:
            raise ValueError("node_id and threshold in [0,1] are required")
        if self.refractory_ms < 0:
            raise ValueError("refractory_ms cannot be negative")
