"""Filesystem contract for atomic Supervisor runtime state and steps."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

_ALLOWED_ROLES = {"plan", "exec", "review", "validate", "route", "arbiter"}

class RuntimeStore:
    def __init__(self, runtime_root: str | Path) -> None:
        self.runtime_root = Path(runtime_root)

    def task_root(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        root = self.runtime_root.resolve()
        task_root = (root / task_id).resolve()
        try:
            task_root.relative_to(root)
        except ValueError as exc:
            raise ValueError("runtime path escapes runtime_root") from exc
        return task_root

    def state_path(self, task_id: str) -> Path:
        return self.task_root(task_id) / "state.json"

    def step_path(self, task_id: str, number: int, role: str, kind: str) -> Path:
        if kind not in {"task", "out"}:
            raise ValueError("kind must be 'task' or 'out'")
        if number < 1:
            raise ValueError("step number must be positive")
        if role not in _ALLOWED_ROLES:
            raise ValueError("role is not allowed")
        path = (self.task_root(task_id) / "steps" / f"{number:02d}-{role}.{kind}.json").resolve()
        try:
            path.relative_to(self.task_root(task_id))
        except ValueError as exc:
            raise ValueError("runtime path escapes task root") from exc
        return path

    def write_state(self, task_id: str, payload: Mapping[str, Any]) -> Path:
        path = self.state_path(task_id)
        self._atomic_write_json(path, payload)
        return path

    def load_state(self, task_id: str) -> dict[str, Any] | None:
        path = self.state_path(task_id)
        return None if not path.is_file() else json.loads(path.read_text(encoding="utf-8"))

    def write_step(self, task_id: str, number: int, role: str, kind: str, payload: Mapping[str, Any]) -> Path:
        path = self.step_path(task_id, number, role, kind)
        self._atomic_write_json(path, payload)
        return path

    def read_step(self, task_id: str, number: int, role: str, kind: str) -> dict[str, Any]:
        return json.loads(self.step_path(task_id, number, role, kind).read_text(encoding="utf-8"))

    def _validate_task_id(self, task_id: str) -> None:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("task_id contains unsupported characters")

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise