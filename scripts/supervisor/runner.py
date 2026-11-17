"""The sole process lifecycle authority for Supervisor agents."""
from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .writer_lock import WriterLock

def _collect_partial(process: "subprocess.Popen[str]") -> tuple[str, str]:
    """Whatever a terminated child left in its pipes, so a timeout keeps the partial output."""
    try:
        out, err = process.communicate(timeout=2)
        return out or "", err or ""
    except Exception:  # noqa: BLE001 -- best effort; the timeout itself must still surface
        return "", ""


# on_event(name, t_monotonic, detail): lifecycle events for patch 02 instrumentation.
# Names: process_start, first_stdout, first_stderr, stdout_eof, stderr_eof, child_exit,
# timeout (detail={"stage": ...}), stdin_closed, cleanup_start, cleanup_end,
# cleanup_incomplete. A sink must never raise
# into the process lifecycle, so _emitter swallows its errors.
EventSink = Callable[[str, float, Mapping[str, str]], None]


def _emitter(sink: "EventSink | None", store: dict[str, float] | None = None,
             details: dict[str, dict[str, str]] | None = None) -> Callable[..., None]:
    """Build emit(name, **detail). ``store`` keeps the first time each event name fired."""
    def emit(name: str, **detail: str) -> None:
        now = time.monotonic()
        if store is not None:
            store.setdefault(name, now)
        if details is not None:
            details.setdefault(name, dict(detail))
        if sink is None:
            return
        try:
            sink(name, now, detail)
        except Exception:
            pass
    return emit


@dataclass(frozen=True)
class OutputLimits:
    max_bytes: int = 8 * 1024 * 1024
    max_line_bytes: int = 2 * 1024 * 1024
    max_chunks: int = 16384
    queue_lines: int = 128

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in self.__dict__.values()):
            raise ValueError("output limits must be positive integers")


class OutputLimitExceeded(RuntimeError):
    """Captured output is incomplete; this can never certify successful work."""
    def __init__(self, channel: str, kind: str):
        super().__init__(f"CLI output limit exceeded ({channel}/{kind}); inspect partial execution before retrying")
        self.channel, self.kind = channel, kind
        self.output = self.stderr = ""


@dataclass
class Runner:
    writer_lock: WriterLock
    # First monotonic time of each lifecycle event in the most recent run_capture*
    # call; adapters read it after the call instead of changing the result tuple.
    # Valid because WriterLock admits one writer at a time.
    last_events: dict[str, float] = field(default_factory=dict)
    # A failed bounded cleanup is not permission to start another child.
    cleanup_pending: set[str] = field(default_factory=set)
    output_limits: OutputLimits | None = None
    last_event_details: dict[str, dict[str, str]] = field(default_factory=dict)
    last_output_stats: dict[str, int] = field(default_factory=dict)

    def _limits(self):
        return self.output_limits or (OutputLimits() if os.environ.get("SOPHIA_G2_BOUNDED_OUTPUT") == "1" else None)

    def start(self, task_id: str, command: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, capture_output: bool = False) -> subprocess.Popen[str]:
        if self.cleanup_pending:
            raise RuntimeError("previous process cleanup is unverified; reconcile before retrying")
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
                    env: Mapping[str, str] | None = None, timeout: float | None = None,
                    on_event: EventSink | None = None,
                    deadline_at: float | None = None) -> tuple[int, str, str]:
        if self._limits() is not None and deadline_at is None:
            if timeout is None or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("bounded output requires a finite positive execution timeout")
            deadline_at = time.monotonic() + timeout
        if deadline_at is not None:
            return self._run_deadline_capture(task_id, command, input_text=input_text,
                                              cwd=cwd, env=env, deadline_at=deadline_at,
                                              timeout=timeout, on_event=on_event)
        self.last_events = {}
        self.last_event_details, self.last_output_stats = {}, {}
        emit = _emitter(on_event, self.last_events, self.last_event_details)
        process = self.start(task_id, command, cwd=cwd, env=env, capture_output=True)
        emit("process_start")
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
            emit("child_exit")
            return process.returncode, stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            emit("timeout", stage="hard_deadline")
            emit("cleanup_start")
            self.terminate(task_id, process)
            emit("cleanup_end")
            partial_out, partial_err = _collect_partial(process)
            raise subprocess.TimeoutExpired(command, timeout, output=partial_out, stderr=partial_err)
        finally:
            self.writer_lock.release_completed(task_id)

    def run_capture_streaming(self, task_id: str, command: Sequence[str], *, input_text: str, cwd: str | None = None,
                              env: Mapping[str, str] | None = None, timeout: float | None = None,
                              on_line: Callable[[str], None] | None = None,
                              on_event: EventSink | None = None,
                              deadline_at: float | None = None) -> tuple[int, str, str]:
        """Like run_capture, but invokes on_line(line) as each stdout line arrives instead
        of blocking until the process exits. Used only for interactive --watch mode; the
        default execute() path still uses the simpler, already-validated run_capture.

        stderr is drained concurrently with stdout (a child that fills the stderr pipe
        would otherwise block in write() and stall stdout until the turn deadline).
        ``on_event(name, t_monotonic, detail)`` receives lifecycle events; see _emitter."""
        if self._limits() is not None and deadline_at is None:
            if timeout is None or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("bounded output requires a finite positive execution timeout")
            deadline_at = time.monotonic() + timeout
        if deadline_at is not None:
            return self._run_deadline_capture(task_id, command, input_text=input_text,
                                              cwd=cwd, env=env, deadline_at=deadline_at,
                                              timeout=timeout, on_line=on_line, on_event=on_event)
        self.last_events = {}
        self.last_event_details, self.last_output_stats = {}, {}
        emit = _emitter(on_event, self.last_events, self.last_event_details)
        process = self.start(task_id, command, cwd=cwd, env=env, capture_output=True)
        emit("process_start")
        line_queue: queue.Queue[str | None] = queue.Queue()
        err_chunks: list[str] = []

        def reader() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    line_queue.put(line)
            finally:
                emit("stdout_eof")
                line_queue.put(None)

        def err_reader() -> None:
            try:
                if process.stderr is None:
                    return
                for chunk in process.stderr:
                    if not err_chunks:
                        emit("first_stderr")
                    err_chunks.append(chunk)
            finally:
                emit("stderr_eof")

        reader_thread = threading.Thread(target=reader, daemon=True)
        err_thread = threading.Thread(target=err_reader, daemon=True)
        try:
            reader_thread.start()
            err_thread.start()
            if process.stdin is not None:
                process.stdin.write(input_text)
                process.stdin.close()
            lines: list[str] = []
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    item = line_queue.get(timeout=remaining)
                except queue.Empty:
                    emit("timeout", stage="hard_deadline")
                    emit("cleanup_start")
                    self.terminate(task_id, process)
                    emit("cleanup_end")
                    err_thread.join(timeout=1)
                    raise subprocess.TimeoutExpired(command, timeout, output="".join(lines),
                                                    stderr="".join(err_chunks))
                if item is None:
                    break
                if not lines:
                    emit("first_stdout")
                lines.append(item)
                if on_line is not None:
                    on_line(item.rstrip("\n"))
            process.wait(timeout=5)
            emit("child_exit")
            err_thread.join(timeout=5)
            return process.returncode, "".join(lines), "".join(err_chunks)
        finally:
            self.writer_lock.release_completed(task_id)

    def _run_deadline_capture(self, task_id: str, command: Sequence[str], *, input_text: str,
                              cwd: str | None, env: Mapping[str, str] | None,
                              deadline_at: float, timeout: float | None,
                              on_line: Callable[[str], None] | None = None,
                              on_event: EventSink | None = None) -> tuple[int, str, str]:
        """One absolute execution deadline for startup, input, output and exit.

        Pipe readers and the stdin writer run concurrently so a child which never
        reads stdin cannot pin the controlling thread. Cleanup has its own bounded
        budget; it never grants the child more execution time after expiry.
        OS process creation itself is synchronous: if it returns late, the child
        is reaped immediately without sending the prompt.
        """
        self.last_events = {}
        self.last_event_details, self.last_output_stats = {}, {}
        emit = _emitter(on_event, self.last_events, self.last_event_details)
        if not math.isfinite(deadline_at):
            raise ValueError("execution deadline must be finite")
        if timeout is not None:
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("timeout must be positive and finite")
            deadline_at = min(deadline_at, time.monotonic() + timeout)
        if deadline_at <= time.monotonic():
            emit("timeout", stage="before_process_start")
            raise subprocess.TimeoutExpired(command, timeout, output="", stderr="")

        process = self.start(task_id, command, cwd=cwd, env=env, capture_output=True)
        emit("process_start")
        limits = self._limits()
        messages: queue.Queue[tuple[str, str | BaseException | None]] = queue.Queue(
            maxsize=limits.queue_lines if limits else 0)
        out_chunks: list[str] = []
        err_chunks: list[str] = []
        stop = threading.Event()
        capture_lock = threading.Lock()
        failures: list[BaseException] = []
        stats = {"stdout_bytes": 0, "stderr_bytes": 0, "chunks": 0, "queue_peak": 0}

        def deliver(kind, value):
            if limits is None:
                messages.put((kind, value))
                return
            while not stop.is_set() and time.monotonic() < deadline_at:
                try:
                    messages.put((kind, value), timeout=.02)
                    stats["queue_peak"] = max(stats["queue_peak"], messages.qsize())
                    return
                except queue.Full:
                    continue

        def retain(channel, chunks, text):
            if limits is None:
                chunks.append(text)
                return
            size = len(text.encode("utf-8"))
            with capture_lock:
                if stats["stdout_bytes"] + stats["stderr_bytes"] + size > limits.max_bytes:
                    raise OutputLimitExceeded(channel, "bytes")
                if stats["chunks"] >= limits.max_chunks:
                    raise OutputLimitExceeded(channel, "chunks")
                stats[f"{channel}_bytes"] += size
                stats["chunks"] += 1
                chunks.append(text)

        def read_pipe(pipe, channel: str, chunks: list[str]) -> None:
            try:
                if limits is None:
                    for line in pipe:
                        if not chunks:
                            emit(f"first_{channel}")
                        retain(channel, chunks, line)
                        if channel == "stdout":
                            deliver("line", line)
                else:
                    parts, line_size = [], 0
                    read = pipe.readline
                    while not stop.is_set() and not failures:
                        text = read(65536)
                        if not text:
                            break
                        if not chunks:
                            emit(f"first_{channel}")
                        retain(channel, chunks, text)
                        if channel == "stdout":
                            line_size += len(text.encode("utf-8"))
                            if line_size > limits.max_line_bytes:
                                raise OutputLimitExceeded(channel, "line_bytes")
                            parts.append(text)
                            if text.endswith("\n"):
                                deliver("line", "".join(parts))
                                parts, line_size = [], 0
                    if parts and not failures and not stop.is_set():
                        deliver("line", "".join(parts))
            except Exception as exc:
                with capture_lock:
                    if not failures:
                        failures.append(exc)
                deliver("error", exc)
            finally:
                pipe.close()
                emit(f"{channel}_eof")
                deliver(channel, None)

        def write_input() -> None:
            try:
                if time.monotonic() >= deadline_at:
                    return
                process.stdin.write(input_text)
                process.stdin.close()
                emit("stdin_closed")
            except (BrokenPipeError, OSError):
                # A provider can exit normally without consuming all input.
                pass
            except Exception as exc:
                deliver("error", exc)
            finally:
                if not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                deliver("stdin", None)

        threads = [threading.Thread(target=read_pipe, args=(process.stdout, "stdout", out_chunks),
                                    name=f"sophia-{task_id}-stdout", daemon=True),
                   threading.Thread(target=read_pipe, args=(process.stderr, "stderr", err_chunks),
                                    name=f"sophia-{task_id}-stderr", daemon=True),
                   threading.Thread(target=write_input, name=f"sophia-{task_id}-stdin", daemon=True)]
        started_threads: list[threading.Thread] = []
        complete: set[str] = set()
        try:
            if deadline_at <= time.monotonic():
                raise subprocess.TimeoutExpired(command, timeout)
            for thread in threads:
                thread.start()
                started_threads.append(thread)
            while True:
                if failures:
                    raise failures[0]
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                if complete == {"stdin", "stdout", "stderr"}:
                    process.wait(timeout=remaining)
                    emit("child_exit")
                    return process.returncode, "".join(out_chunks), "".join(err_chunks)
                try:
                    kind, value = messages.get(timeout=remaining)
                except queue.Empty:
                    raise subprocess.TimeoutExpired(command, timeout) from None
                if kind == "line":
                    if on_line is not None:
                        on_line(value.rstrip("\n"))
                elif kind == "error":
                    raise value
                else:
                    complete.add(kind)
        except BaseException as exc:
            stop.set()
            if isinstance(exc, subprocess.TimeoutExpired):
                emit("timeout", stage="hard_deadline")
            if isinstance(exc, OutputLimitExceeded):
                emit("output_limit", channel=exc.channel, kind=exc.kind)
            emit("cleanup_start")
            try:
                self._cleanup_deadline_process(process, started_threads)
            except BaseException as cleanup_error:
                self.cleanup_pending.add(task_id)
                emit("cleanup_incomplete")
                raise RuntimeError("process cleanup unverified; do not retry") from cleanup_error
            emit("cleanup_end")
            if isinstance(exc, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(command, timeout, output="".join(out_chunks),
                                                stderr="".join(err_chunks)) from exc
            if isinstance(exc, OutputLimitExceeded):
                exc.output, exc.stderr = "".join(out_chunks), "".join(err_chunks)
            raise
        finally:
            stop.set()
            if limits is not None:
                self.last_output_stats = dict(stats, queue_capacity=limits.queue_lines,
                                               capture_capacity_bytes=limits.max_bytes)
            self.writer_lock.release_completed(task_id)

    def _cleanup_deadline_process(self, process: subprocess.Popen[str],
                                  threads: list[threading.Thread], cleanup_s: float = 5.0) -> None:
        """Reap first, then join pipe threads, all within one cleanup budget."""
        cleanup_at = time.monotonic() + cleanup_s
        if process.poll() is None:
            if self._can_reap_tree(process):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                               capture_output=True, check=False, timeout=cleanup_s)
            else:
                process.kill()
            process.wait(timeout=max(0.0, cleanup_at - time.monotonic()))
        for thread in threads:
            thread.join(timeout=max(0.0, cleanup_at - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise TimeoutError("pipe cleanup did not finish")
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

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
