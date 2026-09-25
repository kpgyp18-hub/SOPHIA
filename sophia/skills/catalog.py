"""Validated SKILL.md catalog ingestion for v0.8.

Catalog documents are data, never executable code. Registration resolves tool
names through the host-owned ToolRegistry and derives risk from immutable tool
metadata before a skill can become visible to routing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sophia.runtime.tool_registry import ToolRegistry
from sophia.skills.registry import Skill, SkillRegistry
from sophia.skills.validator import SkillValidationError


CATEGORIES = frozenset({"core", "development", "review", "planning", "ai", "security", "document", "git"})
CATALOG_STATUSES = frozenset({"draft", "active", "deprecated"})
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}


@dataclass(frozen=True)
class CatalogDocument:
    path: Path
    name: str
    version: str
    description: str
    category: str
    capability_tags: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    requires_review: bool
    status: str
    body: str
    source_hash: str

    @property
    def skill_id(self) -> str:
        return f"catalog:{self.category}/{self.name}"


class CatalogLoader:
    def __init__(self, root: str | Path = "sophia/skill_catalog") -> None:
        self.root = Path(root)

    def load(self) -> list[CatalogDocument]:
        if not self.root.exists():
            return []
        documents = [self.parse(path) for path in sorted(self.root.glob("*/*/SKILL.md"))]
        identifiers = [document.skill_id for document in documents]
        if len(identifiers) != len(set(identifiers)):
            raise SkillValidationError("duplicate catalog skill_id")
        return documents

    def parse(self, path: str | Path) -> CatalogDocument:
        source = Path(path).read_text(encoding="utf-8")
        parts = source.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise SkillValidationError(f"missing YAML frontmatter: {path}")
        metadata = yaml.safe_load(parts[1]) or {}
        if not isinstance(metadata, dict):
            raise SkillValidationError(f"invalid frontmatter mapping: {path}")
        required = {"name", "version", "description", "category", "capability_tags", "allowed_tools", "requires_review", "status"}
        missing = sorted(required - metadata.keys())
        if missing:
            raise SkillValidationError(f"missing catalog fields: {', '.join(missing)}")
        name, version, category, status = map(str, (
            metadata["name"], metadata["version"], metadata["category"], metadata["status"],
        ))
        if not _NAME_RE.fullmatch(name):
            raise SkillValidationError(f"invalid catalog skill name: {name}")
        if not _SEMVER_RE.fullmatch(version):
            raise SkillValidationError(f"invalid semantic version: {version}")
        if category not in CATEGORIES or status not in CATALOG_STATUSES:
            raise SkillValidationError(f"invalid catalog category/status: {category}/{status}")
        capabilities = self._strings(metadata["capability_tags"], "capability_tags")
        tools = self._strings(metadata["allowed_tools"], "allowed_tools")
        if not isinstance(metadata["requires_review"], bool):
            raise SkillValidationError("requires_review must be boolean")
        return CatalogDocument(
            Path(path), name, version, str(metadata["description"]).strip(), category,
            capabilities, tools, metadata["requires_review"], status, parts[2].strip(),
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _strings(value: Any, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise SkillValidationError(f"{field} must be a list of strings")
        return tuple(item.strip() for item in value)


class CatalogAdapter:
    def __init__(self, registry: SkillRegistry, tools: ToolRegistry) -> None:
        self.registry, self.tools = registry, tools

    def register(self, document: CatalogDocument) -> Skill:
        specs = [self.tools.get(name) for name in document.allowed_tools]
        computed_risk = max((spec.risk for spec in specs), key=_RISK_ORDER.get, default="R0")
        risk = "low" if _RISK_ORDER[computed_risk] <= 1 else "medium" if computed_risk == "R2" else "high"
        current = self.registry.get(document.skill_id, include_disabled=True)
        if current is not None:
            if self._version(document.version) < self._version(current.version):
                raise SkillValidationError(f"catalog downgrade rejected: {current.version} -> {document.version}")
            old_hash = str(current.metadata.get("source_hash", ""))
            if document.version == current.version:
                if old_hash and old_hash != document.source_hash:
                    raise SkillValidationError("same-version catalog hash conflict")
                if old_hash == document.source_hash:
                    return current
        status = {"draft": "candidate", "active": "active", "deprecated": "disabled"}[document.status]
        skill = Skill(
            skill_id=document.skill_id, name=document.name, version=document.version,
            description=document.description, owner="catalog", risk=risk, status=status,
            skill_path=str(document.path), metadata={
                "category": document.category, "capability_tags": list(document.capability_tags),
                "allowed_tools": list(document.allowed_tools), "requires_review": document.requires_review,
                "computed_risk": computed_risk, "source_hash": document.source_hash,
            },
        )
        return self.registry.register(skill)

    def sync(self, loader: CatalogLoader) -> list[Skill]:
        return [self.register(document) for document in loader.load()]

    @staticmethod
    def _version(version: str) -> tuple[int, int, int]:
        match = _SEMVER_RE.fullmatch(version)
        if match is None:
            raise SkillValidationError(f"invalid semantic version: {version}")
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
