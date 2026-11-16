"""Typed contracts for v0.10.1 artifact context packs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ContextTrust(StrEnum):
    SYSTEM = "SYSTEM"
    INTERNAL = "INTERNAL"
    PROJECT_APPROVED = "PROJECT_APPROVED"
    TRUSTED_EXTERNAL = "TRUSTED_EXTERNAL"
    MODEL_GENERATED_CANDIDATE = "MODEL_GENERATED_CANDIDATE"
    UNTRUSTED = "UNTRUSTED"


class ContextStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class ContextPack:
    context_id: str
    name: str
    version: str
    scope: str
    category: str
    trust: ContextTrust
    priority: int
    max_tokens: int
    content: str
    content_hash: str
    artifact_types: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    status: ContextStatus = ContextStatus.ACTIVE
    source_path: str | None = None
    quality_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_judge(self) -> bool:
        return self.scope == "judge" or self.context_id.startswith("context.judge.")

    def to_record(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id, "name": self.name, "version": self.version,
            "scope": self.scope, "category": self.category, "trust": self.trust.value,
            "priority": self.priority, "max_tokens": self.max_tokens, "content": self.content,
            "content_hash": self.content_hash, "artifact_types": list(self.artifact_types),
            "skills": list(self.skills), "projects": list(self.projects),
            "triggers": list(self.triggers), "requires": list(self.requires),
            "conflicts_with": list(self.conflicts_with), "status": self.status.value,
            "source_path": self.source_path, "quality_score": self.quality_score,
            "metadata": self.metadata,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ContextPack":
        return cls(
            context_id=str(record["context_id"]), name=str(record["name"]),
            version=str(record["version"]), scope=str(record["scope"]),
            category=str(record["category"]), trust=ContextTrust(str(record["trust"]).upper()),
            priority=int(record["priority"]), max_tokens=int(record["max_tokens"]),
            content=str(record["content"]), content_hash=str(record["content_hash"]),
            artifact_types=tuple(record.get("artifact_types", ())), skills=tuple(record.get("skills", ())),
            projects=tuple(record.get("projects", ())), triggers=tuple(record.get("triggers", ())),
            requires=tuple(record.get("requires", ())), conflicts_with=tuple(record.get("conflicts_with", ())),
            status=ContextStatus(str(record.get("status", "active")).lower()),
            source_path=record.get("source_path"), quality_score=float(record.get("quality_score", 0.0)),
            metadata=dict(record.get("metadata", {})),
        )


@dataclass(frozen=True)
class ContextRequest:
    task_type: str
    artifact_type: str
    skill_id: str
    query: str = ""
    project_id: str | None = None
    workflow_id: str | None = None
    explicit_contexts: tuple[str, ...] = ()
    judge: bool = False
    token_budget: int = 4_000


@dataclass(frozen=True)
class ResolvedContext:
    request: ContextRequest
    selected: tuple[ContextPack, ...]
    selection_reasons: dict[str, tuple[str, ...]]
    token_cost: int
    conflicts: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)

