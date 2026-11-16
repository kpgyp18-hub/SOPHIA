"""Stable lineage records for context selections."""
from __future__ import annotations

from dataclasses import dataclass

from sophia.context.models import ResolvedContext


@dataclass(frozen=True)
class ContextProvenance:
    context_id: str
    version: str
    selection_reason: tuple[str, ...]
    content_hash: str

    def to_record(self) -> dict[str, object]:
        return {
            "context_id": self.context_id, "context_version": self.version,
            "selection_reason": list(self.selection_reason), "content_hash": self.content_hash,
        }


def context_provenance(resolved: ResolvedContext) -> tuple[ContextProvenance, ...]:
    return tuple(
        ContextProvenance(
            pack.context_id, pack.version,
            resolved.selection_reasons.get(pack.context_id, ()), pack.content_hash,
        )
        for pack in resolved.selected
    )

