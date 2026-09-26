from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sophia.database.adapters import PostgreSQLBackend, SQLiteBackend
from sophia.database.base import DatabaseBackend, Health


class DatabaseManager:
    """Controlled PostgreSQL-first persistence with an explicit SQLite fallback."""
    def __init__(self, config: dict[str, Any] | None = None, *, root: str | Path = "workspace/sophia") -> None:
        data = config or {}
        backends = data.get("backends", {})
        self.active = str(data.get("active", "postgresql"))
        self.fallback = str(data.get("fallback", "sqlite"))
        self.backends: dict[str, DatabaseBackend] = {
            "postgresql": PostgreSQLBackend(dict(backends.get("postgresql", {}))),
            "sqlite": SQLiteBackend(backends.get("sqlite", {}).get("path", Path(root) / "sophia.db")),
        }

    def health(self) -> dict[str, Health]:
        return {name: backend.health() for name, backend in self.backends.items()}

    def selected(self) -> DatabaseBackend:
        preferred = self.backends.get(self.active)
        if preferred is not None and preferred.health().ok:
            return preferred
        fallback = self.backends.get(self.fallback)
        if fallback is None or not fallback.health().ok:
            raise RuntimeError("no healthy persistent database backend")
        return fallback

    def record(self, category: str, payload: dict[str, Any]) -> str:
        backend = self.selected()
        backend.record(category, payload)
        return backend.name

    def save_session(self, session_id: str, *, status: str, supervisor: str | None = None,
                     brain: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        backend = self.selected()
        payload = metadata or {}
        save = getattr(backend, "save_session", None)
        if callable(save):
            save(session_id, status=status, supervisor=supervisor, brain=brain, metadata=payload)
        else:
            backend.record("runtime.session", {"session_id": session_id, "status": status, **payload})
        return backend.name

    def save_task(self, task_id: str, *, session_id: str, status: str, task_type: str,
                  supervisor: str, brain: str, complexity: float,
                  metadata: dict[str, Any] | None = None) -> str:
        backend = self.selected()
        payload = metadata or {}
        save = getattr(backend, "save_task", None)
        if callable(save):
            save(task_id, session_id=session_id, status=status, task_type=task_type,
                 supervisor=supervisor, brain=brain, complexity=complexity, metadata=payload)
        else:
            backend.record("runtime.task", {"task_id": task_id, "session_id": session_id,
                           "status": status, "task_type": task_type, "supervisor": supervisor,
                           "brain": brain, "complexity": complexity, **payload})
        return backend.name

    def save_context_summary(self, session_id: str, summary: str, *,
                             metadata: dict[str, Any] | None = None) -> str:
        backend = self.selected()
        save = getattr(backend, "save_context_summary", None)
        if not callable(save):
            raise RuntimeError(f"{backend.name} does not support context summaries")
        save(session_id, summary, metadata=metadata or {})
        return backend.name

    def load_context_summary(self, session_id: str) -> dict[str, Any] | None:
        backend = self.selected()
        load = getattr(backend, "load_context_summary", None)
        if not callable(load):
            return None
        return load(session_id)

    def save_goal(self, goal: dict[str, Any], *, event_type: str,
                  event_payload: dict[str, Any] | None = None) -> str:
        backend = self.selected()
        backend.save_goal(goal, event_type=event_type, event_payload=event_payload or {})
        return backend.name

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        return self.selected().get_goal(goal_id)

    def list_goals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return self.selected().list_goals(status=status)

    def save_goal_task(self, task: dict[str, Any], *, event_type: str,
                       event_payload: dict[str, Any] | None = None) -> str:
        backend = self.selected()
        backend.save_goal_task(task, event_type=event_type, event_payload=event_payload or {})
        return backend.name

    def get_goal_task(self, task_id: str) -> dict[str, Any] | None:
        return self.selected().get_goal_task(task_id)

    def list_goal_tasks(self, goal_id: str) -> list[dict[str, Any]]:
        return self.selected().list_goal_tasks(goal_id)

    def save_goal_runtime_record(self, kind: str, record_id: str, payload: dict[str, Any], *,
                                 goal_id: str | None = None, task_id: str | None = None,
                                 status: str | None = None) -> str:
        backend = self.selected()
        backend.save_goal_runtime_record(kind, record_id, payload, goal_id=goal_id,
                                         task_id=task_id, status=status)
        return backend.name

    def get_goal_runtime_record(self, kind: str, record_id: str) -> dict[str, Any] | None:
        return self.selected().get_goal_runtime_record(kind, record_id)

    def list_goal_runtime_records(self, kind: str, *, goal_id: str | None = None,
                                  task_id: str | None = None,
                                  status: str | None = None) -> list[dict[str, Any]]:
        return self.selected().list_goal_runtime_records(
            kind, goal_id=goal_id, task_id=task_id, status=status,
        )

    def delete_goal_runtime_record(self, kind: str, record_id: str) -> None:
        self.selected().delete_goal_runtime_record(kind, record_id)

    def status_lines(self) -> list[str]:
        return [f"{name.upper():<10} {'OK' if health.ok else 'DOWN'} {health.detail}" + (" pgvector=OK" if health.vector_ready else "") for name, health in self.health().items()]

    def backup(self, path: str | Path) -> Path:
        backend = self.selected()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"source": backend.name, "created_at": datetime.now(timezone.utc).isoformat(), "records": backend.export()}, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def restore(self, path: str | Path) -> str:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("invalid database backup")
        backend = self.selected()
        backend.import_records(records)
        return backend.name

    def migrate(self, target: str, backup_path: str | Path) -> str:
        if target not in self.backends:
            raise ValueError(f"unknown backend: {target}")
        source = self.selected()
        target_backend = self.backends[target]
        if source is target_backend:
            target_backend.migrate()
            self.active = target
            return f"migrated {target} schema in place"
        backup = self.backup(backup_path)
        try:
            target_backend.migrate()
            target_backend.import_records(source.export())
        except Exception:
            self.active = source.name
            raise
        self.active = target
        return f"migrated {source.name} -> {target}; backup: {backup}"
