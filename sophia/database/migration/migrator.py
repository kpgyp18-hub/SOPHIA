"""Idempotent PostgreSQL migration runners."""
from __future__ import annotations

from sophia.database.schema import CORE_SCHEMA_STATEMENTS, VECTOR_SCHEMA_STATEMENTS


class PostgreSQLMigrator:
    """Apply v0.4 Core DDL in one database transaction."""

    def migrate(self, connection: object) -> None:
        with connection.cursor() as cursor:  # type: ignore[attr-defined]
            for statement in CORE_SCHEMA_STATEMENTS:
                cursor.execute(statement)

    def migrate_vector(self, connection: object) -> None:
        """Apply the opt-in pgvector schema after its server files are installed."""
        with connection.cursor() as cursor:  # type: ignore[attr-defined]
            for statement in VECTOR_SCHEMA_STATEMENTS:
                cursor.execute(statement)

    @staticmethod
    def rollback_integrity(connection: object, *, critical_tables: tuple[str, ...]) -> dict[str, int]:
        """Return a small post-rollback integrity snapshot for PostgreSQL.

        This is deliberately read-only and takes a fixed caller-supplied list
        rather than accepting arbitrary table names from runtime input.
        It lets migration callers compare row counts before/after a failed
        transaction while the database driver's transaction context performs
        the actual rollback.
        """
        if not critical_tables:
            raise ValueError("at least one critical table is required")
        counts: dict[str, int] = {}
        with connection.cursor() as cursor:  # type: ignore[attr-defined]
            for table in critical_tables:
                if table not in {"runtime.sessions", "runtime.tasks", "system.checkpoints"}:
                    raise ValueError(f"unsupported integrity table: {table}")
                cursor.execute(f"SELECT count(*) FROM {table}")
                row = cursor.fetchone()
                counts[table] = int(row[0])
        return counts
