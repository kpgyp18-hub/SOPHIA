"""Procedural-memory skills for the v0.4 runtime.

Skills are data and validated procedures, never personas or free-form shell
commands.  Their metadata is persisted through :class:`DatabaseManager`.
"""

from sophia.skills.executor import SkillExecution, SkillExecutor
from sophia.skills.registry import Skill, SkillRegistry
from sophia.skills.validator import SkillValidationError, validate_skill

__all__ = [
    "Skill",
    "SkillExecution",
    "SkillExecutor",
    "SkillRegistry",
    "SkillValidationError",
    "validate_skill",
]
