"""Safe local tool primitives used by later Cognitive OS milestones."""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sophia.runtime.permission import ApprovalCallback, PermissionDenied, PermissionLevel, require_approval
from sophia.runtime.risk import RiskLevel, classify_shell, shell_arguments


class ShellResult:
    """Captured result of one policy-approved local shell invocation."""

    def __init__(self, output: str, returncode: int) -> None:
        self.output = output
        self.returncode = returncode


class ToolGateway:
    """Constrain filesystem and shell operations to the explicit workspace root."""

    def __init__(self, workspace: str | Path = ".", *, approval: ApprovalCallback | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.approval = approval

    def _path(self, relative_path: str | Path) -> Path:
        candidate = (self.workspace / relative_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise PermissionDenied("path escapes the workspace")
        return candidate

    def resolve(self, relative_path: str | Path) -> Path:
        """Return a workspace-contained path for a caller that needs a path object."""
        return self._path(relative_path)

    def read_text(self, relative_path: str | Path) -> str:
        return self._path(relative_path).read_text(encoding="utf-8")

    def _audit(self, level: PermissionLevel, operation: str, target: str, *, risk: RiskLevel | None = None) -> None:
        audit_path = self.workspace / ".sophia" / "audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {"at": datetime.now(timezone.utc).isoformat(), "level": int(level), "operation": operation, "target": target}
        if risk is not None:
            event["risk"] = risk.value
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def _backup(self, path: Path) -> Path:
        relative = path.relative_to(self.workspace)
        backup = self.workspace / ".sophia" / "backups" / relative.parent / f"{relative.name}.{uuid4().hex}.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
        return backup

    def write_text(self, relative_path: str | Path, text: str, *, overwrite: bool = False) -> Path | None:
        """Atomically write text; modifications are backed up and logged first.

        ``overwrite=True`` denotes replacement outside the safe diff workflow and is
        a LEVEL 3 operation.  A normal existing-file edit is LEVEL 2.
        """
        path = self._path(relative_path)
        exists = path.exists()
        level = PermissionLevel.DESTRUCTIVE if exists and overwrite else (
            PermissionLevel.MODIFY if exists else PermissionLevel.SAFE_WRITE
        )
        if level is PermissionLevel.DESTRUCTIVE:
            require_approval(self.approval, f"overwrite {relative_path}")
        backup = self._backup(path) if exists else None
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        try:
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        self._audit(level, "write", str(relative_path))
        return backup

    def delete(self, relative_path: str | Path, *, approval_checked: bool = False) -> None:
        path = self._path(relative_path)
        if not approval_checked:
            require_approval(self.approval, f"delete {relative_path}")
        if path.is_dir():
            raise PermissionDenied("directory deletion is not supported")
        if not path.exists():
            raise FileNotFoundError(path)
        self._backup(path)
        path.unlink()
        self._audit(PermissionLevel.DESTRUCTIVE, "delete", str(relative_path), risk=RiskLevel.HIGH)

    def shell_result(self, command: str | list[str], *, allowed: set[str] | None = None) -> ShellResult:
        """Execute through the same safety gate and preserve the process status."""
        arguments = shell_arguments(command)
        if not arguments or allowed is None or arguments[0] not in allowed:
            raise PermissionDenied("shell command is not approved")
        risk = classify_shell(arguments)
        level = PermissionLevel.DESTRUCTIVE if risk is RiskLevel.HIGH else (
            PermissionLevel.MODIFY if risk is RiskLevel.MEDIUM else PermissionLevel.READ
        )
        if level is PermissionLevel.DESTRUCTIVE:
            require_approval(self.approval, f"run shell command: {' '.join(arguments)}")
        completed = subprocess.run(arguments, cwd=self.workspace, text=True, encoding="utf-8", errors="replace",
                                   capture_output=True, check=False, timeout=30)
        self._audit(level, "shell", " ".join(arguments), risk=risk)
        return ShellResult((completed.stdout or "") + (completed.stderr or ""), completed.returncode)

    def shell(self, command: str | list[str], *, allowed: set[str] | None = None) -> str:
        """Compatibility helper for callers interested only in command output."""
        return self.shell_result(command, allowed=allowed).output
