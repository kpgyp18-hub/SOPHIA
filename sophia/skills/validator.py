"""Validation rules for reusable, safe procedural-memory skills."""
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from sophia.runtime.risk import RiskLevel

if TYPE_CHECKING:
    from sophia.skills.registry import Skill


class SkillValidationError(ValueError):
    """Raised when a candidate cannot safely become a reusable skill."""


VALID_STATUSES = frozenset({"candidate", "active", "disabled"})


def validate_skill(skill: "Skill") -> None:
    """Require an explicit, reusable procedure contract.

    The validator intentionally does not execute a procedure.  Execution is a
    separate, handler-only operation in ``SkillExecutor``.
    """
    if not skill.name.strip():
        raise SkillValidationError("skill name is required")
    if not skill.version.strip():
        raise SkillValidationError("skill version is required")
    if skill.status not in VALID_STATUSES:
        raise SkillValidationError(f"unsupported skill status: {skill.status}")
    if skill.risk not in {level.value for level in RiskLevel}:
        raise SkillValidationError(f"unsupported skill risk: {skill.risk}")
    if not skill.description.strip():
        raise SkillValidationError("skill description is required")
    if not isinstance(skill.metadata, dict):
        raise SkillValidationError("skill metadata must be a dictionary")
    if not 0.0 <= skill.quality_score <= 1.0:
        raise SkillValidationError("skill quality_score must be between 0 and 1")
    if skill.quality_samples < 0:
        raise SkillValidationError("skill quality_samples cannot be negative")


def skill_payload(skill: "Skill") -> dict[str, Any]:
    """Return a serializable storage payload after validation."""
    validate_skill(skill)
    return asdict(skill)
