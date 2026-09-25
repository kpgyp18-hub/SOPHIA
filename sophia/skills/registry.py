"""Persistent registry for validated v0.4 procedural-memory skills."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from sophia.database import DatabaseManager
from sophia.skills.validator import SkillValidationError, skill_payload


@dataclass(frozen=True)
class Skill:
    """A reusable procedure descriptor; executable code is registered separately."""

    name: str
    version: str
    description: str
    skill_id: str = field(default_factory=lambda: str(uuid4()))
    owner: str | None = None
    risk: str = "low"
    status: str = "candidate"
    skill_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.0
    quality_samples: int = 0

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Skill":
        return cls(
            skill_id=str(record["skill_id"]), name=str(record["name"]),
            version=str(record["version"]), description=str(record.get("description") or ""),
            owner=record.get("owner"), risk=str(record.get("risk") or "low"),
            status=str(record.get("status") or "candidate"), skill_path=record.get("skill_path"),
            metadata=dict(record.get("metadata") or {}),
            quality_score=float(record.get("quality_score") or 0.0),
            quality_samples=int(record.get("quality_samples") or 0),
        )


class SkillRegistry:
    """Store metadata in ``memory.skills`` and procedures under a local library."""

    def __init__(self, database: DatabaseManager, *, library: str | Path = "sophia/skills/library") -> None:
        self.database = database
        self.library = Path(library)

    def register(self, skill: Skill, *, procedure: str | None = None) -> Skill:
        payload = skill_payload(skill)
        stored_path = skill.skill_path
        if procedure is not None:
            safe_identifier = skill.skill_id.replace(":", "__").replace("/", "__").replace("\\", "__")
            target = self.library / f"{safe_identifier}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(procedure, encoding="utf-8")
            stored_path = str(target)
        if stored_path != skill.skill_path:
            skill = Skill(**{**payload, "skill_path": stored_path})
            payload = skill_payload(skill)
        save = getattr(self.database.selected(), "save_skill", None)
        if not callable(save):
            raise RuntimeError("selected database backend does not support skill storage")
        save(payload)
        return skill

    def get(self, identifier: str, *, include_disabled: bool = False) -> Skill | None:
        find = getattr(self.database.selected(), "get_skill", None)
        if not callable(find):
            raise RuntimeError("selected database backend does not support skill storage")
        record = find(identifier, include_disabled=include_disabled)
        return Skill.from_record(record) if record is not None else None

    def list(self, *, status: str | None = "active") -> list[Skill]:
        find = getattr(self.database.selected(), "list_skills", None)
        if not callable(find):
            raise RuntimeError("selected database backend does not support skill storage")
        return [Skill.from_record(record) for record in find(status=status)]

    def search(self, query: str, *, limit: int = 8) -> list[Skill]:
        """Return bounded active candidates for an explicit task query.

        This is intentionally lexical.  Selection is separate from execution,
        and a candidate still needs an active host handler and its preconditions
        to pass before it can run.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        terms = {term for term in query.casefold().split() if term}
        if not terms:
            return []
        candidates: list[tuple[int, Skill]] = []
        for skill in self.list(status="active"):
            haystack = f"{skill.name} {skill.description}".casefold()
            score = sum(term in haystack for term in terms)
            if score:
                candidates.append((score, skill))
        return [skill for _score, skill in sorted(candidates, key=lambda item: (-item[0], item[1].name))[:limit]]

    def disable(self, identifier: str) -> Skill:
        skill = self.get(identifier, include_disabled=True)
        if skill is None:
            raise KeyError(identifier)
        disabled = Skill(**{**skill_payload(skill), "status": "disabled"})
        return self.register(disabled)

    def record_outcome(self, identifier: str, verdict: str, *, task_id: str | None = None,
                       reason: str | None = None, window: int = 20) -> Skill:
        skill = self.get(identifier, include_disabled=True)
        if skill is None:
            raise KeyError(identifier)
        record = getattr(self.database.selected(), "record_skill_outcome", None)
        if not callable(record):
            raise RuntimeError("selected database backend does not support skill quality tracking")
        record(skill.skill_id, verdict, task_id=task_id, reason=reason, window=window)
        updated = self.get(skill.skill_id, include_disabled=True)
        assert updated is not None
        return updated

    def history(self, identifier: str) -> list[Skill]:
        skill = self.get(identifier, include_disabled=True)
        if skill is None:
            raise KeyError(identifier)
        load = getattr(self.database.selected(), "skill_history", None)
        if not callable(load):
            raise RuntimeError("selected database backend does not support skill history")
        return [Skill.from_record(record) for record in load(skill.skill_id)]

    def procedure(self, skill: Skill) -> str | None:
        if not skill.skill_path:
            return None
        path = Path(skill.skill_path)
        if not path.is_file():
            raise SkillValidationError(f"skill procedure is missing: {path}")
        return path.read_text(encoding="utf-8")
