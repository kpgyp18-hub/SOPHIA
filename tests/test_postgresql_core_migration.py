from __future__ import annotations

from sophia.database.migration import PostgreSQLMigrator
from sophia.database.schema import (
    CORE_SCHEMA_STATEMENTS,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    VECTOR_SCHEMA_STATEMENTS,
)


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchone(self):
        return (7,)


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def test_postgresql_core_migration_contains_no_pgvector_dependency():
    assert CORE_SCHEMA_STATEMENTS
    assert all("vector" not in statement.lower() for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.sessions" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.tasks" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.goals" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.goal_events" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.long_tasks" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.task_dependencies" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("runtime.goal_runtime_records" in statement for statement in CORE_SCHEMA_STATEMENTS)
    assert any("system.checkpoints" in statement for statement in CORE_SCHEMA_STATEMENTS)


def test_postgresql_core_migration_executes_every_idempotent_statement():
    connection = _Connection()
    PostgreSQLMigrator().migrate(connection)
    assert tuple(connection.cursor_instance.statements) == CORE_SCHEMA_STATEMENTS
    assert all("IF NOT EXISTS" in statement for statement in CORE_SCHEMA_STATEMENTS)


def test_pgvector_follow_up_is_explicit_and_fixes_embedding_contract():
    assert EMBEDDING_MODEL == "qwen3-embedding:0.6b"
    assert EMBEDDING_DIMENSIONS == 1024
    assert "CREATE EXTENSION IF NOT EXISTS vector" == VECTOR_SCHEMA_STATEMENTS[0]
    assert any("embedding vector(1024) NOT NULL" in statement for statement in VECTOR_SCHEMA_STATEMENTS)
    assert any("USING hnsw (embedding vector_cosine_ops)" in statement for statement in VECTOR_SCHEMA_STATEMENTS)
    assert all("memory.embeddings" not in statement for statement in CORE_SCHEMA_STATEMENTS)


def test_pgvector_follow_up_executes_only_when_explicitly_requested():
    connection = _Connection()
    PostgreSQLMigrator().migrate_vector(connection)
    assert tuple(connection.cursor_instance.statements) == VECTOR_SCHEMA_STATEMENTS


def test_v071_database_rollback_integrity_uses_a_fixed_read_only_table_allowlist():
    connection = _Connection()
    snapshot = PostgreSQLMigrator.rollback_integrity(
        connection, critical_tables=("runtime.tasks", "system.checkpoints"),
    )
    assert snapshot == {"runtime.tasks": 7, "system.checkpoints": 7}
    assert connection.cursor_instance.statements == [
        "SELECT count(*) FROM runtime.tasks", "SELECT count(*) FROM system.checkpoints",
    ]
