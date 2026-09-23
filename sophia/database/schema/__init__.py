"""Versioned database schema definitions."""

from sophia.database.schema.postgresql import (
    CORE_SCHEMA_STATEMENTS,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    VECTOR_SCHEMA_STATEMENTS,
)

__all__ = [
    "CORE_SCHEMA_STATEMENTS",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
    "VECTOR_SCHEMA_STATEMENTS",
]
