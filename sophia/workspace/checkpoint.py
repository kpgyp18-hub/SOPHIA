"""Immutable file checkpoints for safe workspace edits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class CheckpointType(StrEnum):
    FILE = "file"
    GIT = "git"
    DATABASE = "database"
    CONFIG = "config"


@dataclass(frozen=True)
class Checkpoint:
    id: str
    type: CheckpointType
    target: str
    created_at: str
    existed: bool
    snapshot: str | None
    sha256: str | None
    mtime_ns: int | None = None
    size: int | None = None
    status: str = "active"

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["type"] = self.type.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Checkpoint":
        return cls(id=str(data["id"]), type=CheckpointType(str(data["type"])), target=str(data["target"]),
                   created_at=str(data["created_at"]), existed=bool(data["existed"]),
                   snapshot=str(data["snapshot"]) if data.get("snapshot") is not None else None,
                   sha256=str(data["sha256"]) if data.get("sha256") is not None else None,
                   mtime_ns=int(data["mtime_ns"]) if data.get("mtime_ns") is not None else None,
                   size=int(data["size"]) if data.get("size") is not None else None,
                   status=str(data.get("status", "active")))


def create_file_checkpoint(workspace: str | Path, relative_path: str | Path) -> Checkpoint:
    """Persist a pre-image (or explicit absent marker) before modifying one file."""
    root = Path(workspace).resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("checkpoint target escapes workspace")
    if target.exists() and not target.is_file():
        raise ValueError("file checkpoint target must be a file")
    checkpoint_id = uuid4().hex
    checkpoint_dir = root / ".sophia" / "checkpoints" / checkpoint_id
    checkpoint_dir.mkdir(parents=True, exist_ok=False)
    existed, snapshot, digest, mtime_ns, size = target.exists(), None, None, None, None
    if existed:
        content = target.read_bytes()
        snapshot, digest = "before.bin", hashlib.sha256(content).hexdigest()
        stat = target.stat()
        mtime_ns, size = stat.st_mtime_ns, stat.st_size
        (checkpoint_dir / snapshot).write_bytes(content)
    checkpoint = Checkpoint(checkpoint_id, CheckpointType.FILE, target.relative_to(root).as_posix(),
                            datetime.now(timezone.utc).isoformat(), existed, snapshot, digest, mtime_ns, size)
    (checkpoint_dir / "checkpoint.json").write_text(json.dumps(checkpoint.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return checkpoint


def load_checkpoint(workspace: str | Path, checkpoint_id: str) -> Checkpoint:
    path = Path(workspace).resolve() / ".sophia" / "checkpoints" / checkpoint_id / "checkpoint.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise KeyError(f"checkpoint not found: {checkpoint_id}") from error
    if not isinstance(data, dict):
        raise ValueError(f"invalid checkpoint metadata: {checkpoint_id}")
    return Checkpoint.from_dict(data)


def update_status(workspace: str | Path, checkpoint: Checkpoint, status: str) -> Checkpoint:
    updated = Checkpoint(**{**checkpoint.__dict__, "status": status})
    path = Path(workspace).resolve() / ".sophia" / "checkpoints" / checkpoint.id / "checkpoint.json"
    path.write_text(json.dumps(updated.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return updated
