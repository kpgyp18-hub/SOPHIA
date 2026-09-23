"""Small cross-process lock for checkpoint state changes."""
from __future__ import annotations

import os
from pathlib import Path


class WorkspaceLockedError(RuntimeError):
    """Raised when another workspace operation owns the checkpoint lock."""


class WorkspaceLock:
    """An advisory exclusive lock with no stale-lock deletion policy."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise WorkspaceLockedError(f"workspace checkpoint lock is held: {self.path}") from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self._held = True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> "WorkspaceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
