from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sophia.database.base import Health


class SQLiteBackend:
    """Portable development/fallback backend; never stores credentials."""
    name = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def migrate(self) -> None:
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sophia_events (id INTEGER PRIMARY KEY, category TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_events_category ON sophia_events(category)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_skills (skill_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, version TEXT NOT NULL, owner TEXT, risk TEXT, status TEXT NOT NULL, description TEXT, skill_path TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_session_summaries (summary_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, summary TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_session_summaries_session ON sophia_session_summaries(session_id, summary_id DESC)")

    def health(self) -> Health:
        try:
            self.migrate()
            with self._connect() as db:
                db.execute("SELECT 1").fetchone()
            return Health(self.name, True, str(self.path), False)
        except sqlite3.Error as error:
            return Health(self.name, False, str(error), False)

    def record(self, category: str, payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            db.execute("INSERT INTO sophia_events(category, payload) VALUES (?, ?)", (category, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def export(self) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db:
            rows = db.execute("SELECT category, payload, created_at FROM sophia_events ORDER BY id").fetchall()
        return [{"category": category, "payload": json.loads(payload), "created_at": created_at} for category, payload, created_at in rows]

    def import_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.record(str(record["category"]), dict(record["payload"]))

    def save_skill(self, skill: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sophia_skills(skill_id, name, version, owner, risk, status, description, skill_path, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(skill_id) DO UPDATE SET name=excluded.name, version=excluded.version, owner=excluded.owner, "
                "risk=excluded.risk, status=excluded.status, description=excluded.description, skill_path=excluded.skill_path, "
                "metadata=excluded.metadata, updated_at=CURRENT_TIMESTAMP",
                (skill["skill_id"], skill["name"], skill["version"], skill.get("owner"), skill.get("risk"),
                 skill["status"], skill.get("description"), skill.get("skill_path"),
                 json.dumps(skill.get("metadata", {}), ensure_ascii=False)),
            )

    def get_skill(self, identifier: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata FROM sophia_skills WHERE (skill_id = ? OR name = ?)"
        parameters: tuple[str, ...] = (identifier, identifier)
        if not include_disabled:
            query += " AND status != 'disabled'"
        with self._connect() as db:
            row = db.execute(query, parameters).fetchone()
        return self._skill_record(row) if row else None

    def list_skills(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata FROM sophia_skills"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY name, version"
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [self._skill_record(row) for row in rows]

    def save_context_summary(self, session_id: str, summary: str, *, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sophia_session_summaries(session_id, summary, metadata) VALUES (?, ?, ?)",
                (session_id, summary, json.dumps(metadata, ensure_ascii=False)),
            )

    def load_context_summary(self, session_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db:
            row = db.execute(
                "SELECT summary, metadata FROM sophia_session_summaries WHERE session_id = ? ORDER BY summary_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"summary": row[0], "metadata": json.loads(row[1])}

    @staticmethod
    def _skill_record(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("skill_id", "name", "version", "owner", "risk", "status", "description", "skill_path", "metadata")
        record = dict(zip(keys, row, strict=True))
        record["metadata"] = json.loads(record["metadata"])
        return record
