"""Host-owned, bounded long-running task execution (patch 04).

A chat turn that launches a long job ties the job's life to the turn: when the turn,
the relay or the browser goes away the job dies with it (or is orphaned).  This module
moves an *approved* long job out of the turn.  ``LongTaskHost.start`` spawns a detached
watcher process that owns the job:

  * runs the command with stdout/stderr going to files (no pipe to fill or lose);
  * enforces a hard, explicit deadline and kills the whole process tree on expiry;
  * honours a cancel request (marker file) with the same tree kill;
  * writes the outcome atomically to ``state.json`` so any later process can collect it.

It is gated by the ``SOPHIA_LONG_TASKS=1`` feature flag (off by default) and is NOT wired
into scripts/chat.py or the Supervisor: nothing calls it until a later, separately
approved change does.  Stdlib only, so the watcher can run as a plain script.

Limits stated up front: the watcher survives its launcher exiting, but not a host
reboot or an enclosing Windows job object that kills its children on close; a task whose
watcher died is reported ``lost`` (unknown), never guessed as success or failure.

Rollback: delete this file and test_long_task.py.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

FLAG_ENV = "SOPHIA_LONG_TASKS"
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "workspace" / "long_tasks"
MAX_DEADLINE_S = 4 * 3600
MAX_CONCURRENT = 1
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_POLL_S = 0.2
_STILL_ACTIVE = 259


class LongTasksDisabled(RuntimeError):
    pass


class LongTaskError(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get(FLAG_ENV) == "1"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_state(task_dir: Path, state: dict[str, Any]) -> None:
    tmp = task_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, task_dir / "state.json")


def _read_state(task_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int | None) -> None:
    """Terminate ``pid`` and its descendants (taskkill /T on Windows, process group on POSIX)."""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return
    import signal
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def _wait_gone(pid: int | None, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


class LongTaskHost:
    def __init__(self, root: str | Path | None = None, *, max_concurrent: int = MAX_CONCURRENT,
                 max_deadline_s: float = MAX_DEADLINE_S) -> None:
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.max_concurrent = max_concurrent
        self.max_deadline_s = max_deadline_s

    # ---- API -------------------------------------------------------------------------------

    def start(self, task_id: str, command: Sequence[str], *, deadline_s: float, cwd: str | None = None) -> dict[str, Any]:
        if not enabled():
            raise LongTasksDisabled(f"long tasks are disabled; set {FLAG_ENV}=1 to enable")
        if not _ID_RE.match(task_id or ""):
            raise LongTaskError("task_id must match [A-Za-z0-9_.-]{1,64}")
        if not command:
            raise LongTaskError("command must be a non-empty argument list")
        if not (0 < deadline_s <= self.max_deadline_s):
            raise LongTaskError(f"deadline_s must be in (0, {self.max_deadline_s:g}]; an explicit bound is required")
        self.root.mkdir(parents=True, exist_ok=True)
        running = [t for t in self.list_tasks() if t["status"] == "RUNNING"]
        if len(running) >= self.max_concurrent:
            raise LongTaskError(f"concurrency limit reached ({self.max_concurrent} running)")
        task_dir = self.root / task_id
        task_dir.mkdir(exist_ok=False)  # never overwrite an earlier task's evidence
        spec = {"task_id": task_id, "command": list(command), "cwd": cwd, "deadline_s": float(deadline_s)}
        (task_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        _write_state(task_dir, {"task_id": task_id, "status": "RUNNING", "outcome": None, "exit_code": None,
                                "watcher_pid": None, "child_pid": None, "command": list(command), "cwd": cwd,
                                "deadline_s": float(deadline_s), "started_at": _now()})
        watcher_pid = self._spawn_watcher(task_dir)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            state = _read_state(task_dir) or {}
            if state.get("child_pid") or state.get("status") == "FINISHED":
                return state
            time.sleep(0.05)
        kill_tree(watcher_pid)
        raise LongTaskError("watcher did not report a started child within 10s")

    def status(self, task_id: str) -> dict[str, Any]:
        task_dir = self._dir(task_id)
        state = _read_state(task_dir)
        if state is None:
            raise LongTaskError(f"no state for task {task_id!r}")
        if state["status"] == "RUNNING" and not pid_alive(state.get("watcher_pid")) and state.get("watcher_pid"):
            # The owner is gone and never wrote an outcome: report that honestly.
            child_alive = pid_alive(state.get("child_pid"))
            state = dict(state, status="LOST", outcome="lost", child_still_running=child_alive)
        return state

    def collect(self, task_id: str, *, tail_chars: int = 4000) -> dict[str, Any]:
        state = self.status(task_id)
        task_dir = self._dir(task_id)
        result = dict(state, finished=state["status"] != "RUNNING")
        for name in ("stdout", "stderr"):
            path = task_dir / f"{name}.log"
            result[f"{name}_path"] = str(path)
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            result[f"{name}_bytes"] = len(data)
            result[f"{name}_tail"] = data[-tail_chars:].decode("utf-8", errors="replace")
        return result

    def cancel(self, task_id: str) -> dict[str, Any]:
        state = self.status(task_id)
        if state["status"] == "FINISHED":
            return state
        task_dir = self._dir(task_id)
        (task_dir / "cancel").write_text(_now(), encoding="utf-8")
        if state["status"] == "LOST":
            # No watcher left to honour the marker: reap the child directly.
            kill_tree(state.get("child_pid"))
            gone = _wait_gone(state.get("child_pid"))
            final = {k: v for k, v in state.items() if k != "child_still_running"}
            final.update(status="FINISHED", outcome="cancelled", ended_at=_now(),
                         cleanup="child_gone" if gone else "unconfirmed")
            _write_state(task_dir, final)
            return final
        end = time.monotonic() + 15
        while time.monotonic() < end:
            state = self.status(task_id)
            if state["status"] != "RUNNING":
                return state
            time.sleep(0.1)
        return state

    def list_tasks(self) -> list[dict[str, Any]]:
        out = []
        if not self.root.exists():
            return out
        for task_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            try:
                out.append(self.status(task_dir.name))
            except LongTaskError:
                continue
        return out

    # ---- internals -------------------------------------------------------------------------

    def _dir(self, task_id: str) -> Path:
        if not _ID_RE.match(task_id or ""):
            raise LongTaskError("task_id must match [A-Za-z0-9_.-]{1,64}")
        return self.root / task_id

    @staticmethod
    def _spawn_watcher(task_dir: Path) -> int:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--watch", str(task_dir)]
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            base = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:
                return subprocess.Popen(cmd, creationflags=base | 0x01000000, **kwargs).pid  # BREAKAWAY_FROM_JOB
            except OSError:
                return subprocess.Popen(cmd, creationflags=base, **kwargs).pid
        return subprocess.Popen(cmd, start_new_session=True, **kwargs).pid


def _watch(task_dir: Path) -> int:
    spec = json.loads((task_dir / "spec.json").read_text(encoding="utf-8"))
    state = _read_state(task_dir) or {}
    state["watcher_pid"] = os.getpid()
    started = time.monotonic()
    deadline = started + float(spec["deadline_s"])
    try:
        with open(task_dir / "stdout.log", "wb") as out, open(task_dir / "stderr.log", "wb") as err:
            kwargs: dict[str, Any] = {"cwd": spec.get("cwd"), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": err}
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            child = subprocess.Popen(spec["command"], **kwargs)
            state["child_pid"] = child.pid
            _write_state(task_dir, state)
            outcome = None
            while True:
                code = child.poll()
                if code is not None:
                    outcome = "completed" if code == 0 else "nonzero_exit"
                    break
                if (task_dir / "cancel").exists():
                    outcome = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    outcome = "timeout"
                    break
                time.sleep(_POLL_S)
            if outcome in ("cancelled", "timeout"):
                kill_tree(child.pid)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            code = child.poll()
            state.update(outcome=outcome, exit_code=code,
                         cleanup="child_gone" if code is not None else "unconfirmed")
    except Exception as exc:  # noqa: BLE001 -- the outcome must always be written
        state.update(outcome="error", exit_code=None, error=f"{type(exc).__name__}: {exc}", cleanup="unconfirmed")
    state.update(status="FINISHED", ended_at=_now(), duration_s=round(time.monotonic() - started, 2))
    _write_state(task_dir, state)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--watch":
        sys.exit(_watch(Path(sys.argv[2])))
    print("usage: long_task.py --watch <task_dir>  (internal; use LongTaskHost)", file=sys.stderr)
    sys.exit(2)
