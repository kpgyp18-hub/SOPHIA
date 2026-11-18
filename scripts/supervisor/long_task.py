"""Host-owned, bounded long-running task execution (patch 04).

A chat turn that launches a long job ties the job's life to the turn: when the turn,
the relay or the browser goes away the job dies with it (or is orphaned).  This module
moves an *approved* long job out of the turn.  ``LongTaskHost.start`` spawns a detached
watcher process that owns the job:

  * runs the command with stdout/stderr going to files (no pipe to fill or lose);
  * enforces a hard, explicit deadline and kills the whole process tree on expiry;
  * honours a cancel request (marker file) with the same tree kill;
  * writes the outcome atomically to ``state.json`` so any later process can collect it.

It is gated by the ``SOPHIA_LONG_TASKS=1`` feature flag (off by default).
scripts/long_tasks.py exposes explicitly selected, approved test targets only;
the chat and Supervisor never start detached jobs automatically. Stdlib only.

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
import tempfile
import threading
import time
from contextlib import contextmanager
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
_ADMISSION_THREAD_LOCK = threading.Lock()


class LongTasksDisabled(RuntimeError):
    pass


class LongTaskError(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get(FLAG_ENV) == "1"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_state(task_dir: Path, state: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=task_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    target = task_dir / "state.json"
    if os.name == "nt" and target.exists():
        import ctypes
        from ctypes import wintypes
        replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        replace.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
        replace.restype = wintypes.BOOL
        if not replace(str(target), str(name), None, 0, None, None):
            # Do not retry an unknown partial replacement or remove its evidence.
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(name, target)


def _open_state(path: Path):
    """Allow an atomic rename while a Windows reader holds the prior snapshot.

    CRT file readers omit FILE_SHARE_DELETE and can make a concurrent
    os.replace fail with WinError 5, even though neither operation lacks ACLs.
    """
    if os.name != "nt":
        return path.open("r", encoding="utf-8")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "r", encoding="utf-8")


def _read_state(task_dir: Path) -> dict[str, Any] | None:
    try:
        with _open_state(task_dir / "state.json") as reader:
            state = json.load(reader)
        return state if isinstance(state, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Treat ERROR_INVALID_PARAMETER as the invalid/missing PID case.
            # Access denied or any unknown error is not proof of death.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # unknown liveness must not release a slot
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
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


@contextmanager
def _admission(root: Path):
    """An OS-owned lock is released even when the reserving host dies.

    Keep the lock file: unlinking a locked inode would let new callers lock a
    different file. The process lock also serializes separate host instances.
    """
    if not _ADMISSION_THREAD_LOCK.acquire(timeout=2):
        raise LongTaskError("long-task admission is busy")
    handle = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = (root / ".admission.lock").open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        until = time.monotonic() + 2
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= until:
                    raise LongTaskError("long-task admission is locked by another process") from exc
                time.sleep(0.02)
        yield
    finally:
        try:
            if handle is not None:
                # Closing the descriptor releases the OS lock, including on errors.
                handle.close()
        finally:
            _ADMISSION_THREAD_LOCK.release()


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
        if not _ID_RE.fullmatch(task_id or "") or task_id in (".", ".."):
            raise LongTaskError("task_id must match [A-Za-z0-9_.-]{1,64}")
        if not command:
            raise LongTaskError("command must be a non-empty argument list")
        if not (0 < deadline_s <= self.max_deadline_s):
            raise LongTaskError(f"deadline_s must be in (0, {self.max_deadline_s:g}]; an explicit bound is required")
        task_dir = self.root / task_id
        with _admission(self.root):
            running = 0
            for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
                state = self.status(directory.name)  # unreadable/unknown state fails closed
                if state["status"] == "LOST" or (state["status"] == "FINISHED"
                        and state.get("cleanup") not in ("child_gone", "operator_verified")):
                    raise LongTaskError(f"task {directory.name!r} needs reconciliation before new work")
                running += state["status"] == "RUNNING"
            if running >= self.max_concurrent:
                raise LongTaskError(f"concurrency limit reached ({self.max_concurrent} running)")
            task_dir.mkdir(exist_ok=False)  # reserve before releasing the admission lock
            spec = {"task_id": task_id, "command": list(command), "cwd": cwd, "deadline_s": float(deadline_s)}
            (task_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            _write_state(task_dir, {"task_id": task_id, "status": "RUNNING", "outcome": None, "exit_code": None,
                                "starter_pid": os.getpid(),
                                "watcher_pid": None, "child_pid": None, "command": list(command), "cwd": cwd,
                                "deadline_s": float(deadline_s), "started_at": _now()})
        try:
            watcher_pid = self._spawn_watcher(task_dir)
        except OSError as exc:
            state = _read_state(task_dir) or {"task_id": task_id}
            state.update(status="FINISHED", outcome="error", cleanup="child_gone",
                         ended_at=_now(), error=f"watcher did not start: {exc}")
            _write_state(task_dir, state)
            raise LongTaskError(f"watcher did not start: {exc}") from exc
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
        if state is None or state.get("status") not in ("RUNNING", "FINISHED", "LOST"):
            raise LongTaskError(f"unreadable or unknown state for task {task_id!r}; reconcile before new work")
        owner = state.get("watcher_pid") or state.get("starter_pid")
        if state["status"] == "RUNNING" and not pid_alive(owner):
            # The watcher persists FINISHED immediately before exiting. A reader
            # can hold the preceding RUNNING snapshot while observing its death.
            latest = _read_state(task_dir)
            if latest is not None and latest.get("status") == "FINISHED":
                return latest
            # The owner is gone and never wrote an outcome: report that honestly.
            child_alive = pid_alive(state.get("child_pid"))
            state = dict(state, status="LOST", outcome="lost", child_still_running=child_alive)
        return state

    def reconcile(self, task_id: str, *, note: str) -> dict[str, Any]:
        """An operator confirms process and side-effect inspection; never kills work."""
        if not enabled():
            raise LongTasksDisabled(f"long tasks are disabled; set {FLAG_ENV}=1 to enable")
        if not note.strip():
            raise LongTaskError("a reconciliation note is required")
        task_dir = self._dir(task_id)
        if not task_dir.is_dir():
            raise LongTaskError(f"unknown task {task_id!r}")
        with _admission(self.root):
            state = _read_state(task_dir) or {"task_id": task_id}
            if any(pid_alive(state.get(key)) for key in ("watcher_pid", "child_pid")):
                raise LongTaskError("task process is still alive; stop it and inspect its effects first")
            if state.get("status") == "RUNNING" and pid_alive(state.get("starter_pid")):
                raise LongTaskError("task launcher is still alive")
            if state.get("status") == "FINISHED" and state.get("cleanup") in ("child_gone", "operator_verified"):
                raise LongTaskError("task is already settled")
            # Preserve even malformed state before the operator releases admission.
            evidence = task_dir / f"state.before-reconcile-{time.time_ns()}.json"
            if (task_dir / "state.json").exists():
                evidence.write_bytes((task_dir / "state.json").read_bytes())
            state.update(status="FINISHED", outcome="reconciled", cleanup="operator_verified",
                         ended_at=_now(), reconciliation_note=note.strip(), reconciled_by_pid=os.getpid())
            _write_state(task_dir, state)
            return state

    def collect(self, task_id: str, *, tail_chars: int = 4000) -> dict[str, Any]:
        state = self.status(task_id)
        task_dir = self._dir(task_id)
        result = dict(state, finished=state["status"] != "RUNNING")
        result["settled"] = state["status"] == "FINISHED" and state.get("cleanup") in (
            "child_gone", "operator_verified")
        result["requires_reconciliation"] = state["status"] == "LOST" or (
            state["status"] == "FINISHED" and not result["settled"])
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
            except LongTaskError as exc:
                out.append({"task_id": task_dir.name, "status": "UNKNOWN", "outcome": None,
                            "cleanup": "unconfirmed", "error": str(exc)})
        return out

    # ---- internals -------------------------------------------------------------------------

    def _dir(self, task_id: str) -> Path:
        if not _ID_RE.fullmatch(task_id or "") or task_id in (".", ".."):
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
    child = None
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
        state.update(outcome="error", exit_code=None, error=f"{type(exc).__name__}: {exc}",
                     cleanup="child_gone" if child is None else "unconfirmed")
    state.update(status="FINISHED", ended_at=_now(), duration_s=round(time.monotonic() - started, 2))
    _write_state(task_dir, state)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--watch":
        sys.exit(_watch(Path(sys.argv[2])))
    print("usage: long_task.py --watch <task_dir>  (internal; use LongTaskHost)", file=sys.stderr)
    sys.exit(2)
