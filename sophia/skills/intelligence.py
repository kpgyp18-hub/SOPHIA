"""Bounded skill learning: generate candidates, never self-promote."""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable

from sophia.skills.registry import Skill, SkillRegistry
from sophia.skills.validator import skill_payload


class SkillCandidateLearner:
    def __init__(self, registry: SkillRegistry, *, minimum_successes: int = 3) -> None:
        if minimum_successes < 2:
            raise ValueError("minimum_successes must be at least two")
        self.registry, self.minimum_successes = registry, minimum_successes
        self._successes: Counter[str] = Counter()

    def observe_success(self, task_type: str, procedure: str, *, description: str = "Learned successful procedure.") -> Skill | None:
        signature = hashlib.sha256(f"{task_type}\0{procedure}".encode("utf-8")).hexdigest()[:16]
        self._successes[signature] += 1
        identifier = f"learned:{signature}"
        existing = self.registry.get(identifier, include_disabled=True)
        if existing is not None:
            return existing
        if self._successes[signature] < self.minimum_successes:
            return None
        return self.registry.register(Skill(
            skill_id=identifier, name=f"learned-{signature}", version="0.1.0",
            description=description, owner="adaptive-learning", status="candidate", risk="low",
            metadata={"task_type": task_type, "successes": self._successes[signature], "approval_required": True},
        ), procedure=procedure)

    def activate(self, identifier: str, *, approval: Callable[[Skill], bool] | None) -> Skill:
        candidate = self.registry.get(identifier, include_disabled=True)
        if candidate is None:
            raise KeyError(identifier)
        if candidate.status != "candidate":
            raise ValueError("only candidate skills can be activated")
        if approval is None or not approval(candidate):
            raise PermissionError("explicit approval is required to activate a learned skill")
        active = Skill(**{**skill_payload(candidate), "status": "active"})
        return self.registry.register(active)
