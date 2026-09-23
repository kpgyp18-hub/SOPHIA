from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TaskStore:
    """Durable, shared task state; the source of truth for handoffs."""
    def __init__(self, root: str | Path = "workspace/sophia/tasks") -> None:
        self.root = Path(root)

    def write(self, task_id: str, **state: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"task_id": task_id, "updated_at": datetime.now(timezone.utc).isoformat(), **state}
        (self.root / f"{task_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def read(self, task_id: str) -> dict[str, Any] | None:
        """Return one durable task snapshot, if it still exists."""
        path = self.root / f"{task_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        """Merge a change into a task snapshot without losing prior task data."""
        current = self.read(task_id) or {}
        current.pop("task_id", None)
        current.pop("updated_at", None)
        current.update(changes)
        self.write(task_id, **current)
        return self.read(task_id) or {"task_id": task_id, **current}

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest task snapshots without exposing file paths to callers."""
        if limit < 1 or not self.root.is_dir():
            return []
        snapshots: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("task-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                snapshot = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            snapshots.append(snapshot)
            if len(snapshots) >= limit:
                break
        return snapshots
