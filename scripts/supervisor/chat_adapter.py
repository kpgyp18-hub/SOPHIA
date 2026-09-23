"""Free-form chat adapters for the interactive chat REPL (scripts/chat.py)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import tomllib
from pathlib import Path
from typing import Callable, Sequence

from .progress import WaitProgress
from .runner import Runner
from .stream_text import extract_chat_final_text
from .timing import TimingLog, TurnTiming
from .usage import TokenUsage, UsageLog, extract_token_usage


class ChatAdapterError(RuntimeError):
    pass


class ChatAdapterInterrupted(RuntimeError):
    """A user supplied a steering instruction while this CLI turn was running."""


def format_resolved_model(agent) -> str | None:
    """Human-readable model label for chat display. An explicit override or a
    value the CLI itself reported this turn is shown as-is (ground truth). A
    value merely read from local config (Codex has no other source) is
    flagged "(configured)" so it isn't mistaken for a confirmed live result --
    server-side routing, profiles, or per-call overrides can still differ from
    what the config file says. Returns None when nothing is known yet."""
    override = getattr(agent, "model", None)
    if override:
        return override
    resolved = getattr(agent, "resolved_model", None)
    if not resolved:
        return None
    if getattr(agent, "resolved_model_source", None) == "configured":
        return f"{resolved} (configured)"
    return resolved


class ChatAdapter:
    executable = ""
    display_name = ""

    def __init__(self, runner: Runner, *, cwd: str, timeout_seconds: float = 300.0,
                 executable_path: str | None = None, on_line: Callable[[str], None] | None = None,
                 show_progress: bool = True, timing_log: TimingLog | None = None,
                 usage_log: UsageLog | None = None, model: str | None = None,
                 extra_writable_dirs: Sequence[str] | None = None) -> None:
        self.runner = runner
        self.cwd = cwd
        self.extra_writable_dirs = list(extra_writable_dirs or [])
        self.timeout_seconds = timeout_seconds
        self.executable_path = executable_path
        self.on_line = on_line
        self.show_progress = show_progress
        self.timing_log = timing_log or TimingLog(None)
        self.last_timing: TurnTiming | None = None
        self.last_usage: TokenUsage | None = None
        self.usage_log = usage_log or UsageLog(None)
        self.model = model
        # The concrete model ID the provider actually used, when the CLI
        # reports it (live during streaming, or discovered ahead of time for
        # providers that don't). None means "genuinely unknown so far" -- the
        # presenter shows that as "provider default", not a guess.
        self.resolved_model: str | None = None
        # "reported": the CLI itself confirmed this ran this turn (ground truth).
        # "configured": inferred from local config without the CLI confirming
        # it actually ran -- display must say so, not present it as fact.
        self.resolved_model_source: str | None = None
        # True only when this send had to discard an expired remote session.
        # ChatSession uses it to resend a full local context before displaying
        # a reply from the newly-created remote session.
        self.last_reset = False
        self._binary: str | None = None
        self._session_id: str | None = None
        self._active_lock = threading.Lock()
        self._active_task_id: str | None = None
        self._cancelled_task_ids: set[str] = set()

    def reset_session(self) -> None:
        """Forget this adapter's remote conversation identity."""
        self._session_id = None

    def set_model(self, model: str | None) -> bool:
        """Select a model for future turns and invalidate its remote session."""
        normalized = model.strip() if isinstance(model, str) else None
        normalized = normalized or None
        if self.model == normalized:
            return False
        self.model = normalized
        # Stale until the next turn's CLI output (or a subclass override)
        # re-establishes what's actually running.
        self.resolved_model = None
        self.resolved_model_source = None
        self.reset_session()
        return True

    def cancel_current(self) -> bool:
        """Stop the active subprocess so ChatSession can restart it with a steer."""
        with self._active_lock:
            task_id = self._active_task_id
            if task_id is None:
                return False
            self._cancelled_task_ids.add(task_id)
        return self.runner.cancel(task_id)

    def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
        self.last_reset = False
        self.last_usage = None
        self._binary = self._resolve_binary()
        was_resuming = self._session_id is not None
        code, stdout, stderr = self._run(task_id, self._command(), prompt, context=context)
        with self._active_lock:
            was_cancelled = task_id in self._cancelled_task_ids
            self._cancelled_task_ids.discard(task_id)
        if was_cancelled:
            raise ChatAdapterInterrupted("request stopped to apply a new steering instruction")
        if code != 0 and was_resuming and self._is_resume_failure(stdout, stderr):
            # The replacement call has only the caller's delta prompt.  Do not
            # stream that context-incomplete reply; ChatSession will detect
            # last_reset and immediately resend a complete local context.
            self.reset_session()
            self.last_reset = True
            code, stdout, stderr = self._run(task_id, self._command(), prompt, context=context, display=False)
        if code != 0:
            diagnostic = (stderr.strip() or stdout.strip())[-800:]
            raise ChatAdapterError(f"{self.executable} CLI exited with code {code}: {diagnostic}")
        for line in stdout.splitlines():
            self._handle_event_line(line)
        self.last_usage = extract_token_usage(stdout)
        self.usage_log.record(self.display_name, self.last_usage)
        return extract_chat_final_text(stdout)

    def _run(self, task_id: str, command: Sequence[str], prompt: str, *, context: str | None = None,
             display: bool = True) -> tuple[int, str, str]:
        with self._active_lock:
            self._active_task_id = task_id
        suffix = f" ({context})" if context else ""
        progress = WaitProgress(f"{self.display_name} 응답 대기 중{suffix}", enabled=self.show_progress)
        progress.start()
        timing = TurnTiming(label=self.display_name, start=time.monotonic())
        try:
            if self.on_line is not None:
                def on_line_then_stop(line: str) -> None:
                    if timing.first_output is None:
                        timing.first_output = time.monotonic()
                    # Parse metadata (resolved model, session id) as each line
                    # arrives, ahead of the on-completion pass in send(), so a
                    # live-streamed title reflects it on this same turn.
                    self._handle_event_line(line)
                    progress.stop()
                    if display:
                        self.on_line(line)

                return self.runner.run_capture_streaming(
                    task_id, command, input_text=prompt, cwd=self.cwd, timeout=self.timeout_seconds,
                    on_line=on_line_then_stop)
            result = self.runner.run_capture(
                task_id, command, input_text=prompt, cwd=self.cwd, timeout=self.timeout_seconds)
            timing.first_output = time.monotonic()
            return result
        finally:
            timing.end = time.monotonic()
            progress.stop()
            self.last_timing = timing
            self.timing_log.record(timing)
            with self._active_lock:
                if self._active_task_id == task_id:
                    self._active_task_id = None

    def _resolve_binary(self) -> str:
        if self.executable_path:
            return self.executable_path
        path = shutil.which(f"{self.executable}.cmd") if os.name == "nt" else None
        path = path or shutil.which(self.executable)
        if not path:
            raise ChatAdapterError(f"{self.executable} CLI not found")
        return path

    def _command(self) -> Sequence[str]:
        raise NotImplementedError

    def _handle_event_line(self, line: str) -> None:
        """Observe one raw CLI JSONL event (live during streaming, or replayed
        from full stdout for a non-streaming turn) to capture CLI-specific
        details: a resumable conversation identity, and -- where the CLI
        reports it -- the concrete model actually used this turn."""

    @staticmethod
    def _is_resume_failure(stdout: str, stderr: str) -> bool:
        diagnostic = f"{stdout}\n{stderr}".lower()
        return (
            ("resume" in diagnostic or "session" in diagnostic or "thread" in diagnostic)
            and any(marker in diagnostic for marker in ("not found", "does not exist", "unknown", "invalid"))
        )


class ClaudeChatAdapter(ChatAdapter):
    executable = "claude"
    display_name = "Claude"

    def __init__(self, *args, permission_mode: str = "acceptEdits",
                 allowed_tools: str = "Bash Read Write Edit Glob Grep", model: str | None = None, **kwargs) -> None:
        super().__init__(*args, model=model, **kwargs)
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools

    def _command(self) -> Sequence[str]:
        command = [self._binary, "--print", "--permission-mode", self.permission_mode]
        if self.model:
            command += ["--model", self.model]
        if self.allowed_tools:
            command += ["--allowedTools", self.allowed_tools]
        command += ["--add-dir", self.cwd]
        for extra_dir in self.extra_writable_dirs:
            command += ["--add-dir", extra_dir]
        if self._session_id:
            command += ["--resume", self._session_id]
        command += ["--output-format", "stream-json", "--verbose"]
        if self.on_line is not None:
            command += ["--include-partial-messages"]
        return command

    def _handle_event_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        # The startup event names the concrete model this turn actually ran
        # on (e.g. "claude-sonnet-5"), even when no --model override was
        # given -- the CLI's own default choice, not a guess on our side.
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str) and model:
                self.resolved_model = model
                self.resolved_model_source = "reported"
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id


class CodexChatAdapter(ChatAdapter):
    executable = "codex"
    display_name = "Codex"

    def __init__(self, *args, sandbox: str = "workspace-write", model: str | None = None, **kwargs) -> None:
        super().__init__(*args, model=model, **kwargs)
        self.sandbox = sandbox
        # Unlike Claude, Codex's --json turn events never name the model that
        # actually ran. Read its own config for the default it would resolve
        # to, so an unset --model still shows a concrete name instead of a
        # blank "provider default". Best-effort display only: never affects
        # which model the CLI actually picks.
        if self.model is None:
            self.resolved_model = _read_codex_default_model()
            self.resolved_model_source = "configured" if self.resolved_model else None

    def _command(self) -> Sequence[str]:
        command = [self._binary, "exec"]
        if self._session_id:
            command += ["resume", self._session_id]
            # ``exec resume`` intentionally exposes a smaller flag set than
            # ``exec`` (no --sandbox/--cd/--add-dir). Its process cwd is still
            # supplied by Runner; preserve the sandbox policy and any extra
            # writable roots through documented config overrides instead.
            # NOTE: unverified whether writable_roots extends or replaces the
            # default cwd-based root on this Codex build -- confirm with a
            # real write-into-extra-dir test on a resumed (2nd+) turn.
            command += ["--config", f'sandbox_mode="{self.sandbox}"']
            if self.extra_writable_dirs:
                roots = ", ".join(f"'{d}'" for d in self.extra_writable_dirs)
                command += ["--config", f"sandbox_workspace_write.writable_roots=[{roots}]"]
        else:
            command += ["--sandbox", self.sandbox, "--cd", self.cwd]
            for extra_dir in self.extra_writable_dirs:
                command += ["--add-dir", extra_dir]
        command += ["--skip-git-repo-check"]
        if self.model:
            command += ["--model", self.model]
        command += ["--json"]
        return command

    def set_model(self, model: str | None) -> bool:
        changed = super().set_model(model)
        if changed and self.model is None:
            self.resolved_model = _read_codex_default_model()
            self.resolved_model_source = "configured" if self.resolved_model else None
        return changed

    def _handle_event_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            return
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            self._session_id = thread_id


def _read_codex_default_model() -> str | None:
    """Best-effort read of the model Codex's own config would resolve to when
    no --model override is given. Display-only: if this can't be read, the
    adapter still runs fine and simply shows "provider default" instead."""
    home = os.environ.get("CODEX_HOME")
    config_path = Path(home) / "config.toml" if home else Path.home() / ".codex" / "config.toml"
    try:
        with open(config_path, "rb") as config_file:
            data = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model else None
