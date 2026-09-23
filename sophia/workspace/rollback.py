"""Restore a file checkpoint without following paths outside its workspace."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from sophia.workspace.checkpoint import Checkpoint, CheckpointType, load_checkpoint, update_status


def rollback(workspace: str | Path, checkpoint: Checkpoint | str) -> Checkpoint:
    root = Path(workspace).resolve()
    point = load_checkpoint(root, checkpoint) if isinstance(checkpoint, str) else checkpoint
    if point.type is not CheckpointType.FILE:
        raise NotImplementedError(f"rollback is not implemented for {point.type.value} checkpoints")
    target = (root / point.target).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("checkpoint target escapes workspace")
    if point.existed:
        if point.snapshot is None or point.sha256 is None:
            raise ValueError("file checkpoint is missing its snapshot")
        content = (root / ".sophia" / "checkpoints" / point.id / point.snapshot).read_bytes()
        if hashlib.sha256(content).hexdigest() != point.sha256:
            raise ValueError("checkpoint snapshot integrity check failed")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        # The atomic replace is only a delivery mechanism; verify the restored
        # workspace bytes before declaring recovery successful.
        if hashlib.sha256(target.read_bytes()).hexdigest() != point.sha256:
            raise ValueError("rollback target integrity check failed")
    elif target.exists():
        if not target.is_file():
            raise ValueError("rollback will not remove a directory")
        target.unlink()
    return update_status(root, point, "rolled_back")
