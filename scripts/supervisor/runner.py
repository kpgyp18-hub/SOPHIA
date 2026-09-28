"""The sole process lifecycle authority for Supervisor agents."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .writer_lock import WriterLock

@dataclass
class Runner:
    writer_lock: WriterLock

    def start(self, task_id: str, command: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, capture_output: bool = False) -> subprocess.Popen[str]:
        if not self.writer_lock.can_start_next(task_id):
            raise RuntimeError("previous agent process has not exited")
        child_env = os.environ.copy()
        child_env.update(env or {})
        process = subprocess.Popen(list(command), cwd=cwd, env=child_env, text=True,
                                   stdin=subprocess.PIPE if capture_output else None,
                                   stdout=subprocess.PIPE if capture_output else None,
                                   stderr=subprocess.PIPE if capture_output else None, encoding="utf-8", errors="replace")
        self.writer_lock.acquire(task_id, process)
        return process

    def run_capture(self, task_id: str, command: Sequence[str], *, input_text: str, cwd: str | None = None,
                    env: Mapping[str, str] | None = None, timeout: float | None = None) -> tuple[int, str, str]:
        process = self.start(task_id, command, cwd=cwd, env=env, capture_output=True)
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
            return process.returncode, stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            self.terminate(task_id, process)
            raise
        finally:
            self.writer_lock.release_completed(task_id)

    def run_capture_streaming(self, task_id: str, command: Sequence[str], *, input_text: str, cwd: str | None = None,
                              env: Mapping[str, str] | None = None, timeout: float | None = None,
                              on_line: Callable[[str], None] | None = None) -> tuple[int, str, str]:
        """Like run_capture, but invokes on_line(line) as each stdout line arrives instead
        of blocking until the process exits. Used only for interactive --watch mode; the
        default execute() path still uses the simpler, already-validated run_capture."""
        process = self.start(task_id, command, cwd=cwd, env=env, capture_output=True)
        line_queue: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    line_queue.put(line)
            finally:
                line_queue.put(None)

        reader_thread = threading.Thread(target=reader, daemon=True)
        try:
            if process.stdin is not None:
                process.stdin.write(input_text)
                process.stdin.close()
            reader_thread.start()
            lines: list[str] = []
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    item = line_queue.get(timeout=remaining)
                except queue.Empty:
                    self.terminate(task_id, process)
                    raise subprocess.TimeoutExpired(command, timeout)
                if item is None:
                    break
                lines.append(item)
                if on_line is not None:
                    on_line(item.rstrip("\n"))
            process.wait(timeout=5)
            stderr = process.stderr.read() if process.stderr is not None else ""
            return process.returncode, "".join(lines), stderr or ""
        finally:
            self.writer_lock.release_completed(task_id)

    def wait(self, task_id: str, process: subprocess.Popen[str], timeout: float | None = None) -> int:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.terminate(task_id, process)
            raise
        finally:
            self.writer_lock.release_completed(task_id)

    def terminate(self, task_id: str, process: subprocess.Popen[str], grace_timeout: float = 5.0) -> None:
        try:
            if process.poll() is None:
                if self._can_reap_tree(process):
                    # Reap descendants *before* the wrapper dies. TerminateProcess on
                    # cmd.exe re-parents its children, after which taskkill /T has no
                    # tree left to walk: the real CLI process survives as an orphan and
                    # keeps whatever it holds (e.g. Codex's thread-writer lock), so the
                    # next turn's ``exec resume`` collides with it. Keeping the parent
                    # alive is what lets /T reach the whole tree.
                    self._kill_windows_process_tree(process, force=False)
                    try:
                        process.wait(timeout=grace_timeout)
                    except subprocess.TimeoutExpired:
                        # Graceful taskkill is advisory and console CLIs routinely
                        # ignore it; /F is the only reliable reap.
                        self._kill_windows_process_tree(process, force=True)
                        process.kill()
                        process.wait()
                else:
                    # No tree to reap (POSIX, or no pid to walk from): signal the
                    # process directly, exactly as before.
                    process.terminate()
                    try:
                        process.wait(timeout=grace_timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        finally:
            self.writer_lock.release_completed(task_id)

    def cancel(self, task_id: str) -> bool:
        """Terminate the currently registered process for an interactive steer."""
        process = self.writer_lock.active(task_id)
        if process is None or process.poll() is not None:
            return False
        self.terminate(task_id, process)
        return True

    @staticmethod
    def _can_reap_tree(process: subprocess.Popen[str]) -> bool:
        """Whether taskkill /T can actually walk this process's descendants. When it
        can't, terminate() must stay the graceful step -- otherwise nothing signals
        the process at all and cleanup degrades to a grace-period stall plus kill."""
        return os.name == "nt" and bool(getattr(process, "pid", None))

    @staticmethod
    def _kill_windows_process_tree(process: subprocess.Popen[str], *, force: bool) -> None:
        """process.terminate()/kill() only signal the direct child on Windows; CLI
        wrappers (claude.cmd/codex.cmd) spawn further processes that would otherwise
        be orphaned. taskkill /T reaps the whole tree; /F is added on the final,
        post-grace-period escalation. Callers must invoke this while the wrapper is
        still alive -- /T resolves descendants through live parentage."""
        if os.name != "nt":
            return
        pid = getattr(process, "pid", None)
        if not pid:
            return
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        subprocess.run(args, capture_output=True, check=False)
