"""Task-local loop budgets for sequential cross-agent handoffs."""
from collections import Counter
from dataclasses import dataclass, field

MAX_AGENT_HANDOFFS = 4
MAX_CROSS_AGENT_ROUNDTRIPS = 4
MAX_SAME_REASON_HANDOFFS = 2

@dataclass
class LoopGuard:
    current_agent: str | None = None
    agent_handoffs: int = 0
    cross_agent_roundtrips: int = 0
    same_reason_count: Counter[str] = field(default_factory=Counter)
    agent_history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.agent_history and self.current_agent is not None:
            self.agent_history.append(self.current_agent)

    def can_handoff(self, target_agent: str, reason_code: str) -> tuple[bool, str | None]:
        if self.agent_handoffs >= MAX_AGENT_HANDOFFS:
            return False, "NEED_USER"
        if self.same_reason_count[reason_code] >= MAX_SAME_REASON_HANDOFFS:
            return False, "FAILED"
        if _roundtrips(self.agent_history + [target_agent]) > MAX_CROSS_AGENT_ROUNDTRIPS:
            return False, "NEED_USER"
        return True, None

    def record_handoff(self, target_agent: str, reason_code: str) -> tuple[bool, str | None]:
        allowed, status = self.can_handoff(target_agent, reason_code)
        if allowed:
            self.agent_handoffs += 1
            self.same_reason_count[reason_code] += 1
            self.agent_history.append(target_agent)
            self.cross_agent_roundtrips = _roundtrips(self.agent_history)
        return allowed, status

def _roundtrips(agents: list[str]) -> int:
    return sum(1 for i in range(2, len(agents)) if agents[i] == agents[i - 2] and agents[i] != agents[i - 1])