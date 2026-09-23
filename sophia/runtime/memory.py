"""Small dependency-free SQLite project memory with lexical retrieval (v0.4)."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


EMBEDDING_MODEL = "qwen3-embedding:0.6b"
EMBEDDING_DIMENSION = 1024
EMBEDDING_VERSION = "v1"
SYSTEM_DECAY_FLOOR = 0.2
DECAY_HALF_LIFE_DAYS = 30.0
MERGE_SIMILARITY = 0.95
CONSOLIDATION_SIMILARITY = 0.80


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class ProjectMemory:
    """Bound active retrieval memory while preserving older entries in an archive."""

    def __init__(self, path: str | Path = "workspace/sophia/memory.db", *, max_entries: int = 1_000,
                 embedder: Embedder | None = None) -> None:
        self.path = Path(path)
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self.embedder = embedder
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, task_id TEXT, text TEXT NOT NULL, embedding TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS memories_archive (id INTEGER PRIMARY KEY, task_id TEXT, text TEXT NOT NULL, archived_at TEXT NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
            for column, definition in {
                "embedding": "TEXT", "source_type": "TEXT NOT NULL DEFAULT 'system'", "source_id": "TEXT",
                "session_id": "TEXT", "supervisor": "TEXT", "brain": "TEXT", "provider": "TEXT",
                "confidence": "REAL NOT NULL DEFAULT 1.0", "trust_level": "TEXT NOT NULL DEFAULT 'SYSTEM'",
                "created_at": "TEXT", "updated_at": "TEXT", "version": "INTEGER NOT NULL DEFAULT 1",
                "embedding_model": "TEXT", "embedding_dimension": "INTEGER", "embedding_version": "TEXT",
                "importance": "REAL NOT NULL DEFAULT 0.5", "access_count": "INTEGER NOT NULL DEFAULT 0",
                "last_accessed_at": "TEXT",
            }.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")
            archive_columns = {row[1] for row in db.execute("PRAGMA table_info(memories_archive)")}
            for column, definition in {
                "source_type": "TEXT NOT NULL DEFAULT 'system'", "source_id": "TEXT", "session_id": "TEXT",
                "supervisor": "TEXT", "brain": "TEXT", "provider": "TEXT", "confidence": "REAL NOT NULL DEFAULT 1.0",
                "trust_level": "TEXT NOT NULL DEFAULT 'SYSTEM'", "created_at": "TEXT", "updated_at": "TEXT",
                "version": "INTEGER NOT NULL DEFAULT 1", "embedding_model": "TEXT", "embedding_dimension": "INTEGER",
                "embedding_version": "TEXT",
                "importance": "REAL NOT NULL DEFAULT 0.5", "access_count": "INTEGER NOT NULL DEFAULT 0",
                "last_accessed_at": "TEXT", "consolidated_into": "INTEGER",
            }.items():
                if column not in archive_columns:
                    db.execute(f"ALTER TABLE memories_archive ADD COLUMN {column} {definition}")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def add(self, task_id: str, text: str, *, source_type: str = "system", source_id: str | None = None,
            session_id: str | None = None, supervisor: str | None = None, brain: str | None = None,
            provider: str | None = None, confidence: float = 1.0, trust_level: str = "SYSTEM",
            version: int = 1) -> None:
        """Persist memory with its origin and embedding-space contract."""
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        embedding = self._embed(text)
        now = datetime.now(timezone.utc).isoformat()
        dimensions = len(embedding) if embedding is not None else None
        importance = self._initial_importance(confidence, trust_level)
        with self._connect() as db:
            duplicate_id = self._merge_target(db, task_id, embedding)
            if duplicate_id is not None:
                db.execute(
                    "UPDATE memories SET confidence = MAX(confidence, ?), importance = MAX(importance, ?), "
                    "updated_at = ? WHERE id = ?",
                    (confidence, importance, now, duplicate_id),
                )
                return
            db.execute(
                "INSERT INTO memories(task_id, text, embedding, source_type, source_id, session_id, supervisor, brain, provider, confidence, trust_level, created_at, updated_at, version, embedding_model, embedding_dimension, embedding_version, importance, access_count, last_accessed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, text, json.dumps(embedding) if embedding is not None else None, source_type,
                 source_id or task_id, session_id, supervisor, brain, provider, confidence, trust_level, now, now,
                 version, EMBEDDING_MODEL if embedding is not None else None, dimensions,
                 EMBEDDING_VERSION if embedding is not None else None, importance, 0, now),
            )
            self._archive_overflow(db)

    def search(self, query: str, limit: int = 3) -> list[str]:
        if limit < 1:
            return []
        query_embedding = self._embed(query)
        terms = set(re.findall(r"\w+", query.lower()))
        with self._connect() as db:
            rows = db.execute("SELECT id, text, embedding, embedding_model, embedding_dimension, embedding_version FROM memories ORDER BY id DESC LIMIT 200").fetchall()
        semantic: list[tuple[float, int, str]] = []
        if query_embedding is not None:
            for entry_id, text, raw_embedding, model, dimension, version in rows:
                if raw_embedding is None or (model, dimension, version) != (EMBEDDING_MODEL, len(query_embedding), EMBEDDING_VERSION):
                    continue
                try:
                    score = self._cosine(query_embedding, json.loads(raw_embedding))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                semantic.append((score, entry_id, text))
        if semantic:
            selected = [(entry_id, text) for score, entry_id, text in sorted(semantic, reverse=True) if score > 0][:limit]
        else:
            ranked = sorted(((len(terms & set(re.findall(r"\w+", text.lower()))), entry_id, text)
                             for entry_id, text, *_ in rows), reverse=True)
            selected = [(entry_id, text) for score, entry_id, text in ranked if score][:limit]
        if selected:
            now = datetime.now(timezone.utc).isoformat()
            with self._connect() as db:
                db.executemany("UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
                               [(now, entry_id) for entry_id, _text in selected])
        return [text for _entry_id, text in selected]

    def consolidate(self) -> int:
        """Archive near-duplicate active memories behind a deterministic summary entry.

        This maintenance operation never destroys a source record: consolidated
        rows retain their provenance in ``memories_archive.consolidated_into``.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, task_id, text, embedding, confidence, trust_level FROM memories "
                "WHERE embedding IS NOT NULL ORDER BY task_id, id"
            ).fetchall()
            grouped: dict[str, list[tuple]] = {}
            for row in rows:
                grouped.setdefault(str(row[1]), []).append(row)
            consolidated = 0
            for task_id, candidates in grouped.items():
                consumed: set[int] = set()
                for anchor in candidates:
                    anchor_id, _task, _text, raw_embedding, _confidence, _trust = anchor
                    if anchor_id in consumed:
                        continue
                    try:
                        vector = json.loads(raw_embedding)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    cluster = [anchor]
                    for candidate in candidates:
                        if candidate[0] == anchor_id or candidate[0] in consumed:
                            continue
                        try:
                            similarity = self._cosine(vector, json.loads(candidate[3]))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if CONSOLIDATION_SIMILARITY <= similarity < MERGE_SIMILARITY:
                            cluster.append(candidate)
                    if len(cluster) < 2:
                        continue
                    now = datetime.now(timezone.utc).isoformat()
                    summary = "\n".join(dict.fromkeys(str(item[2]) for item in cluster))
                    confidence = max(float(item[4]) for item in cluster)
                    trust_level = max((str(item[5]) for item in cluster), key=self._trust_weight)
                    db.execute(
                        "INSERT INTO memories(task_id, text, embedding, confidence, trust_level, created_at, updated_at, "
                        "embedding_model, embedding_dimension, embedding_version, importance, access_count, last_accessed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (task_id, summary, raw_embedding, confidence, trust_level, now, now, EMBEDDING_MODEL,
                         len(vector), EMBEDDING_VERSION, self._initial_importance(confidence, trust_level), 0, now),
                    )
                    summary_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                    self._archive_ids(db, [int(item[0]) for item in cluster], consolidated_into=summary_id)
                    consumed.update(int(item[0]) for item in cluster)
                    consolidated += len(cluster)
            self._archive_overflow(db)
            return consolidated

    def _merge_target(self, db: sqlite3.Connection, task_id: str, embedding: list[float] | None) -> int | None:
        if embedding is None:
            return None
        rows = db.execute(
            "SELECT id, embedding, embedding_model, embedding_dimension, embedding_version FROM memories WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        for entry_id, raw_embedding, model, dimension, version in rows:
            if raw_embedding is None or (model, dimension, version) != (EMBEDDING_MODEL, len(embedding), EMBEDDING_VERSION):
                continue
            try:
                if self._cosine(embedding, json.loads(raw_embedding)) >= MERGE_SIMILARITY:
                    return int(entry_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return None

    def _archive_overflow(self, db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT id, importance, trust_level, last_accessed_at, created_at FROM memories").fetchall()
        if len(rows) <= self.max_entries:
            return
        overflow = sorted(rows, key=lambda row: (self._effective_importance(*row[1:]), row[0]))[:len(rows) - self.max_entries]
        self._archive_ids(db, [int(row[0]) for row in overflow])

    def _archive_ids(self, db: sqlite3.Connection, identifiers: list[int], *, consolidated_into: int | None = None) -> None:
        if not identifiers:
            return
        placeholders = ",".join("?" for _ in identifiers)
        archived_at = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO memories_archive(id, task_id, text, archived_at, source_type, source_id, session_id, supervisor, brain, provider, confidence, trust_level, created_at, updated_at, version, embedding_model, embedding_dimension, embedding_version, importance, access_count, last_accessed_at, consolidated_into) "
            f"SELECT id, task_id, text, ?, source_type, source_id, session_id, supervisor, brain, provider, confidence, trust_level, created_at, updated_at, version, embedding_model, embedding_dimension, embedding_version, importance, access_count, last_accessed_at, ? FROM memories WHERE id IN ({placeholders})",
            (archived_at, consolidated_into, *identifiers),
        )
        db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", identifiers)

    @staticmethod
    def _trust_weight(trust_level: str) -> float:
        return {"SYSTEM": 1.0, "TOOL_VERIFIED": 0.9, "USER": 0.8, "EXTERNAL": 0.4}.get(trust_level.upper(), 0.5)

    @classmethod
    def _initial_importance(cls, confidence: float, trust_level: str) -> float:
        return (confidence + cls._trust_weight(trust_level)) / 2

    @classmethod
    def _effective_importance(cls, importance: float, trust_level: str, last_accessed_at: str | None,
                              created_at: str | None) -> float:
        timestamp = last_accessed_at or created_at
        if not timestamp:
            return float(importance)
        try:
            then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return float(importance)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86_400)
        decayed = float(importance) * (0.5 ** (age_days / DECAY_HALF_LIFE_DAYS))
        return max(SYSTEM_DECAY_FLOOR, decayed) if trust_level.upper() == "SYSTEM" else decayed

    def embedding_status(self) -> str:
        """Report whether stored vectors can be compared with the active model."""
        with self._connect() as db:
            rows = db.execute("SELECT embedding_model, embedding_dimension, embedding_version FROM memories WHERE embedding IS NOT NULL").fetchall()
        return "REINDEX_REQUIRED" if any(row != (EMBEDDING_MODEL, EMBEDDING_DIMENSION, EMBEDDING_VERSION) for row in rows) else "READY"

    def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            vector = self.embedder.embed(text)
        except (OSError, RuntimeError, TimeoutError, ValueError):
            # Retrieval remains available through the bounded lexical fallback
            # when the local embedding model is busy or unavailable.
            return None
        if not vector:
            return None
        return vector

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            raise ValueError("incompatible embedding dimensions")
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
        return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0
