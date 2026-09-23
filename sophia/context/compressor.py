"""Database persistence for bounded session-context summaries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sophia.database import DatabaseManager


@dataclass(frozen=True)
class ContextSummary:
    session_id: str
    summary: str
    metadata: dict[str, Any]


class ContextCompressor:
    """Store compacted context in PostgreSQL first, with SQLite fallback."""

    def __init__(self, database: DatabaseManager) -> None:
        self.database = database

    def save(self, session_id: str, summary: str, *, metadata: dict[str, Any] | None = None) -> str:
        return self.database.save_context_summary(session_id, summary, metadata=metadata)

    def load(self, session_id: str) -> ContextSummary | None:
        record = self.database.load_context_summary(session_id)
        if record is None:
            return None
        return ContextSummary(
            session_id=session_id,
            summary=str(record.get("summary", "")),
            metadata=dict(record.get("metadata") or {}),
        )
