"""Bounded observations only. No observation grants permission or settles work."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

SCOPE = ("Two git-status-visible worktree snapshots, status plus size/mtime metadata only; "
         "excludes ignored paths, DB/network effects, committed, transient or restored writes. "
         "Observed changes are not attributed exclusively to this attempt.")
_MAX_STATUS_BYTES = 1024 * 1024
_MAX_PATHS = 4096


def capability(adapter) -> str:
    """Configured adapter capability, not a proof of filesystem/network isolation."""
    from .chat_adapter import ClaudeChatAdapter, CodexChatAdapter
    try:
        if isinstance(adapter, CodexChatAdapter):
            if type(adapter.network_access) is not bool:
                return "unknown"
            if adapter.sandbox == "read-only" and not adapter.network_access:
                return "read_only"
            if adapter.sandbox == "workspace-write":
                return "workspace_write_network" if adapter.network_access else "workspace_write"
        if isinstance(adapter, ClaudeChatAdapter) and adapter.permission_mode == "dontAsk":
            tools = set(adapter.allowed_tools.replace(",", " ").split())
            known = {"Read", "Glob", "Grep", "Write", "Edit", "MultiEdit", "Bash", "WebFetch", "WebSearch"}
            if not tools or not tools <= known:
                return "unknown"
            if "Bash" in tools:
                return "workspace_write_network"
            writes = bool(tools & {"Write", "Edit", "MultiEdit"})
            network = bool(tools & {"WebFetch", "WebSearch"})
            if writes:
                return "workspace_write_network" if network else "workspace_write"
            if not network:
                return "read_only"
    except Exception:
        pass
    return "unknown"


def unknown_fingerprint(reason: str) -> dict:
    return {"status": "unknown", "reason": reason, "scope": SCOPE,
            "root": None, "digest": None, "files": {}}


def _query(args, cwd, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("git observation budget exhausted")
    # Avoid optional index writes and configured fsmonitor helper execution.
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", *args]
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    # A file-backed sink bounds retained memory even if git reports many paths.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=remaining, check=False)
        if result.returncode:
            raise ValueError("git query failed or workspace is not a repository")
        output.seek(0)
        data = output.read(_MAX_STATUS_BYTES + 1)
    if len(data) > _MAX_STATUS_BYTES:
        raise ValueError("git status output limit")
    return data


def workspace_fingerprint(cwd, timeout_s=2) -> dict:
    """Read-only git/status/stat probe sharing one finite budget (at most 2s).

    No file content is read. Synchronous OS file/process operations themselves
    cannot be preempted; subprocess execution uses the remaining query budget.
    """
    try:
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            return unknown_fingerprint("observation_budget_unavailable")
        deadline = time.monotonic() + min(2.0, timeout_s)
        top = _query(["rev-parse", "--show-toplevel"], cwd, deadline)
        root = Path(os.fsdecode(top).strip()).resolve()
        raw = _query(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd, deadline)
        entries = iter(raw.split(b"\0"))
        statuses = {}
        for entry in entries:
            if not entry:
                continue
            if len(entry) < 4 or entry[2:3] != b" ":
                raise ValueError("malformed status output")
            status, name = entry[:2].decode("ascii"), os.fsdecode(entry[3:])
            statuses[name] = status
            if "R" in status or "C" in status:
                original = next(entries, b"")
                if not original:
                    raise ValueError("missing rename/copy source")
                statuses[os.fsdecode(original)] = "rename_or_copy_source"
            if len(statuses) > _MAX_PATHS:
                raise ValueError("git observation path limit")
        files = {}
        for name, status in statuses.items():
            if time.monotonic() >= deadline:
                raise TimeoutError("git observation budget exhausted")
            if (len(name) > 1024 or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                    or Path(name).is_absolute()):
                raise ValueError("unsupported status path")
            path = root / name
            if not path.resolve().is_relative_to(root):
                raise ValueError("status path resolves outside worktree")
            try:
                value = path.lstat()
                metadata = [value.st_size, value.st_mtime_ns, value.st_mode]
            except FileNotFoundError:
                metadata = None
            files[name] = {"status": status, "metadata": metadata}
        if time.monotonic() >= deadline:
            raise TimeoutError("git observation budget exhausted")
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return {"status": "ok", "scope": SCOPE, "root": str(root), "digest": digest, "files": files}
    except (subprocess.TimeoutExpired, TimeoutError):
        return unknown_fingerprint("git_observation_timeout")
    except Exception:
        # Do not retain environment/command diagnostics or turn observation into
        # a provider failure. An unreadable snapshot is never "none observed".
        return unknown_fingerprint("git_observation_unavailable")


def compare_fingerprints(before: dict, after: dict) -> tuple[str, dict]:
    evidence = {"scope": SCOPE, "worktree": before.get("root"), "paths": [], "truncated": False}
    for name, value in (("before", before), ("after", after)):
        evidence[name] = {key: value.get(key) for key in ("status", "digest", "reason")}
    if (before.get("status") != "ok" or after.get("status") != "ok"
            or before.get("root") != after.get("root")):
        return "unknown", evidence
    old, new = before["files"], after["files"]
    paths = sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))
    evidence.update(paths=paths[:50], changed_paths_count=len(paths), truncated=len(paths) > 50)
    return ("changed" if paths else "none_observed"), evidence
