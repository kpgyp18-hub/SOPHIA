"""Load context definitions from the filesystem definition source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from sophia.context.models import ContextPack, ContextStatus, ContextTrust
from sophia.context.validator import ContextValidationError, validate_content, validate_metadata


class ContextLoader:
    def __init__(self, root: str | Path = "sophia/contexts") -> None:
        self.root = Path(root)

    def load(self) -> list[ContextPack]:
        if not self.root.exists():
            return []
        packs = [self.parse(path) for path in sorted(self.root.glob("**/metadata.yaml"))]
        identifiers = [pack.context_id for pack in packs]
        if len(identifiers) != len(set(identifiers)):
            raise ContextValidationError("duplicate context_id")
        return packs

    def parse(self, metadata_path: str | Path) -> ContextPack:
        path = Path(metadata_path)
        content_path = path.with_name("CONTEXT.md")
        if not content_path.is_file():
            raise ContextValidationError(f"missing CONTEXT.md: {path.parent}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ContextValidationError(f"invalid context metadata mapping: {path}")
        metadata = validate_metadata(raw)
        content = content_path.read_text(encoding="utf-8").strip()
        validate_content(content)
        trust = ContextTrust(str(metadata["trust"]).upper())
        status = ContextStatus(str(metadata["status"]).lower())
        if trust in {ContextTrust.MODEL_GENERATED_CANDIDATE, ContextTrust.UNTRUSTED}:
            status = ContextStatus.QUARANTINED
        applies_to = metadata.get("applies_to", {})
        canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{canonical}\n{content}".encode("utf-8")).hexdigest()
        known = {
            "schema_version", "context_id", "name", "version", "scope", "category", "trust",
            "applies_to", "priority", "max_tokens", "triggers", "requires", "conflicts_with", "status",
        }
        return ContextPack(
            context_id=str(metadata["context_id"]), name=str(metadata["name"]),
            version=str(metadata["version"]), scope=str(metadata["scope"]),
            category=str(metadata["category"]), trust=trust,
            priority=int(metadata["priority"]), max_tokens=int(metadata["max_tokens"]),
            content=content, content_hash=digest,
            artifact_types=tuple(applies_to.get("artifact_types", ())),
            skills=tuple(applies_to.get("skills", ())), projects=tuple(applies_to.get("projects", ())),
            triggers=tuple(metadata.get("triggers", ())), requires=tuple(metadata.get("requires", ())),
            conflicts_with=tuple(metadata.get("conflicts_with", ())), status=status,
            source_path=str(path.parent), quality_score=float(metadata.get("quality_score", 0.0)),
            metadata={key: value for key, value in metadata.items() if key not in known},
        )

