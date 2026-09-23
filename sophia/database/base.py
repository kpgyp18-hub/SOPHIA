from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Health:
    name: str
    ok: bool
    detail: str
    vector_ready: bool = False


class DatabaseBackend(Protocol):
    name: str

    def health(self) -> Health: ...
    def migrate(self) -> None: ...
    def record(self, category: str, payload: dict[str, Any]) -> None: ...
    def export(self) -> list[dict[str, Any]]: ...
    def import_records(self, records: list[dict[str, Any]]) -> None: ...
    def save_context_summary(self, session_id: str, summary: str, *, metadata: dict[str, Any]) -> None: ...
    def load_context_summary(self, session_id: str) -> dict[str, Any] | None: ...
