from __future__ import annotations

import json
import os
from typing import Any

from sophia.database.base import Health
from sophia.database.migration import PostgreSQLMigrator


class PostgreSQLBackend:
    """Lazy psycopg backend. Missing drivers/services are reported, not hidden."""
    name = "postgresql"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _connect(self):
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("psycopg is not installed; install psycopg[binary] before enabling PostgreSQL") from error
        password = self.config.get("password") or os.environ.get("SOPHIA_DB_PASSWORD")
        return psycopg.connect(host=self.config.get("host", "localhost"), port=int(self.config.get("port", 5432)),
                               dbname=self.config.get("database", "sophia"), user=self.config.get("user", "sophia"),
                               password=password, connect_timeout=3)

    def migrate(self) -> None:
        with self._connect() as db, db.cursor() as cursor:
            PostgreSQLMigrator().migrate(db)
            cursor.execute("CREATE TABLE IF NOT EXISTS system.events (id BIGSERIAL PRIMARY KEY, category TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cursor.execute("CREATE INDEX IF NOT EXISTS events_category_idx ON system.events(category)")

    def migrate_vector(self) -> None:
        """Apply the separately gated pgvector schema.

        This intentionally does not run from :meth:`migrate`: installations
        without pgvector must retain a healthy PostgreSQL Core backend.
        """
        with self._connect() as db:
            PostgreSQLMigrator().migrate(db)
            PostgreSQLMigrator().migrate_vector(db)

    def health(self) -> Health:
        try:
            self.migrate()
            with self._connect() as db, db.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                vector_ready = bool(cursor.fetchone()[0])
            return Health(self.name, True, "connected", vector_ready)
        except Exception as error:  # driver/network errors are operational health, not programming errors
            return Health(self.name, False, str(error), False)

    def record(self, category: str, payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute("INSERT INTO system.events(category, payload) VALUES (%s, %s::jsonb)", (category, json.dumps(payload, ensure_ascii=False)))

    def export(self) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute("SELECT category, payload, created_at::text FROM system.events ORDER BY id")
            return [{"category": category, "payload": payload, "created_at": created_at} for category, payload, created_at in cursor.fetchall()]

    def import_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.record(str(record["category"]), dict(record["payload"]))

    def save_session(self, session_id: str, *, status: str, supervisor: str | None,
                     brain: str | None, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.sessions(session_id, status, active_supervisor, active_brain, metadata) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (session_id) DO UPDATE SET status = EXCLUDED.status, "
                "active_supervisor = EXCLUDED.active_supervisor, active_brain = EXCLUDED.active_brain, "
                "metadata = EXCLUDED.metadata, updated_at = now()",
                (session_id, status, supervisor, brain, json.dumps(metadata, ensure_ascii=False)),
            )

    def save_task(self, task_id: str, *, session_id: str, status: str, task_type: str,
                  supervisor: str, brain: str, complexity: float, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.tasks(task_id, session_id, owner, task_type, status, complexity, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (task_id) DO UPDATE SET session_id = EXCLUDED.session_id, owner = EXCLUDED.owner, "
                "task_type = EXCLUDED.task_type, status = EXCLUDED.status, complexity = EXCLUDED.complexity, "
                "metadata = EXCLUDED.metadata, updated_at = now()",
                (task_id, session_id, supervisor, task_type, status, complexity,
                 json.dumps({**metadata, "brain": brain}, ensure_ascii=False)),
            )

    def save_skill(self, skill: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO memory.skills(skill_id, name, version, owner, risk, status, description, skill_path, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (skill_id) DO UPDATE SET name=EXCLUDED.name, version=EXCLUDED.version, owner=EXCLUDED.owner, "
                "risk=EXCLUDED.risk, status=EXCLUDED.status, description=EXCLUDED.description, skill_path=EXCLUDED.skill_path, "
                "metadata=EXCLUDED.metadata, updated_at=now()",
                (skill["skill_id"], skill["name"], skill["version"], skill.get("owner"), skill.get("risk"),
                 skill["status"], skill.get("description"), skill.get("skill_path"),
                 json.dumps(skill.get("metadata", {}), ensure_ascii=False)),
            )

    def get_skill(self, identifier: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata FROM memory.skills WHERE (skill_id = %s OR name = %s)"
        if not include_disabled:
            query += " AND status != 'disabled'"
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(query, (identifier, identifier))
            row = cursor.fetchone()
        return self._skill_record(row) if row else None

    def list_skills(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata FROM memory.skills"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = %s"
            parameters = (status,)
        query += " ORDER BY name, version"
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(query, parameters)
            rows = cursor.fetchall()
        return [self._skill_record(row) for row in rows]

    def save_context_summary(self, session_id: str, summary: str, *, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.session_summaries(session_id, summary, metadata) VALUES (%s, %s, %s::jsonb)",
                (session_id, summary, json.dumps(metadata, ensure_ascii=False)),
            )

    def load_context_summary(self, session_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT summary, metadata FROM runtime.session_summaries WHERE session_id = %s ORDER BY summary_id DESC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {"summary": row[0], "metadata": row[1] if isinstance(row[1], dict) else json.loads(row[1])}

    @staticmethod
    def _skill_record(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("skill_id", "name", "version", "owner", "risk", "status", "description", "skill_path", "metadata")
        return dict(zip(keys, row, strict=True))
