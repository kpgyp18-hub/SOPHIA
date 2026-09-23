"""Explicit, handler-only execution for registered skills."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from sophia.skills.registry import Skill, SkillRegistry

SkillHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class SkillExecution:
    skill_id: str
    status: str
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0


class SkillExecutor:
    """Run only handlers registered by the host application.

    Procedure markdown is documentation and cannot become executable code. This
    keeps a persisted skill from acquiring shell or filesystem authority.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self._handlers: dict[str, SkillHandler] = {}

    def register_handler(self, identifier: str, handler: SkillHandler) -> None:
        if not callable(handler):
            raise TypeError("skill handler must be callable")
        skill = self.registry.get(identifier, include_disabled=True)
        if skill is None:
            raise KeyError(identifier)
        self._handlers[skill.skill_id] = handler

    def execute(self, identifier: str, inputs: dict[str, Any] | None = None) -> SkillExecution:
        skill = self.registry.get(identifier)
        if skill is None:
            return SkillExecution(identifier, "rejected", error="skill is unavailable or disabled")
        payload = dict(inputs or {})
        failed_precondition = self._failed_precondition(skill, payload)
        if failed_precondition is not None:
            return SkillExecution(skill.skill_id, "rejected", error=failed_precondition)
        handler = self._handlers.get(skill.skill_id)
        if handler is None:
            return SkillExecution(skill.skill_id, "rejected", error="no host handler is registered")
        started = perf_counter()
        try:
            result = handler(payload)
        except Exception as error:
            return SkillExecution(skill.skill_id, "failed", error=str(error),
                                  duration_ms=(perf_counter() - started) * 1000)
        return SkillExecution(skill.skill_id, "completed", result=result,
                              duration_ms=(perf_counter() - started) * 1000)

    @staticmethod
    def _failed_precondition(skill, inputs: dict[str, Any]) -> str | None:
        """Evaluate declarative metadata before a host handler is reachable."""
        metadata = skill.metadata
        required = metadata.get("required_inputs", ())
        if not isinstance(required, (list, tuple)) or not all(isinstance(key, str) for key in required):
            return "skill has invalid required_inputs metadata"
        missing = [key for key in required if key not in inputs or inputs[key] is None]
        if missing:
            return f"skill precondition failed: missing inputs {', '.join(missing)}"
        task_types = metadata.get("task_types", ())
        if not isinstance(task_types, (list, tuple)) or not all(isinstance(item, str) for item in task_types):
            return "skill has invalid task_types metadata"
        if task_types and inputs.get("task_type") not in task_types:
            return "skill precondition failed: task type is not allowed"
        return None
