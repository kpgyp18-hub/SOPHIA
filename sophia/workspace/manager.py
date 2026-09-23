"""Checkpoint lifecycle coordinator for file modifications."""
from __future__ import annotations

from pathlib import Path

from sophia.workspace.checkpoint import Checkpoint, create_file_checkpoint, update_status
from sophia.workspace.lock import WorkspaceLock
from sophia.workspace.rollback import rollback


class StaleWorkspaceError(RuntimeError):
    """Raised when a checkpoint target changed after it was checkpointed."""


class WorkspaceManager:
    """Create, commit, or roll back checkpoints under one locked workspace."""

    def __init__(self, workspace: str | Path = ".") -> None:
        self.workspace = Path(workspace).resolve()
        self._lock = WorkspaceLock(self.workspace / ".sophia" / "checkpoint.lock")

    def checkpoint_file(self, relative_path: str | Path) -> Checkpoint:
        with self._lock:
            return create_file_checkpoint(self.workspace, relative_path)

    def commit(self, checkpoint: Checkpoint) -> Checkpoint:
        with self._lock:
            if checkpoint.status != "active":
                raise ValueError(f"checkpoint is not active: {checkpoint.id}")
            return update_status(self.workspace, checkpoint, "committed")

    def assert_fresh(self, checkpoint: Checkpoint) -> None:
        """Block a write if another actor modified its checkpointed pre-image."""
        import hashlib

        with self._lock:
            if checkpoint.status != "active":
                raise ValueError(f"checkpoint is not active: {checkpoint.id}")
            target = (self.workspace / checkpoint.target).resolve()
            if target != self.workspace and self.workspace not in target.parents:
                raise PermissionError("checkpoint target escapes workspace")
            if not checkpoint.existed:
                if target.exists():
                    raise StaleWorkspaceError("STALE_WORKSPACE: target was created after checkpoint")
                return
            if not target.is_file():
                raise StaleWorkspaceError("STALE_WORKSPACE: checkpoint target is missing or not a file")
            stat = target.stat()
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if (digest != checkpoint.sha256 or stat.st_mtime_ns != checkpoint.mtime_ns
                    or stat.st_size != checkpoint.size):
                raise StaleWorkspaceError("STALE_WORKSPACE: checkpoint target changed after checkpoint")

    def rollback(self, checkpoint: Checkpoint) -> Checkpoint:
        with self._lock:
            if checkpoint.status != "active":
                raise ValueError(f"checkpoint is not active: {checkpoint.id}")
            return rollback(self.workspace, checkpoint)
