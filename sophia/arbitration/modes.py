from __future__ import annotations

from enum import StrEnum


class ArbitrationMode(StrEnum):
    NORMAL = "normal"
    FAST = "fast"
    CAUTIOUS = "cautious"
    RESEARCH = "research"
    CRITICAL = "critical"
    RECOVERY = "recovery"


MODE_BIASES: dict[ArbitrationMode, dict[str, float]] = {
    ArbitrationMode.NORMAL: {},
    ArbitrationMode.FAST: {"provider.fast": 0.20, "action.analyze": -0.08},
    ArbitrationMode.CAUTIOUS: {"action.review": 0.20, "safety.checkpoint": 0.20, "action.implement": -0.10},
    ArbitrationMode.RESEARCH: {"action.retrieve": 0.20, "memory.project": 0.20, "action.implement": -0.15},
    ArbitrationMode.CRITICAL: {"safety.deep": 0.30, "safety.approval": 0.30, "supervisor.blanc": 0.20,
                               "action.implement": -0.30, "provider.external": -0.30},
    ArbitrationMode.RECOVERY: {"action.rollback": 0.35, "skill.diagnose": 0.25, "action.implement": -0.30},
}
