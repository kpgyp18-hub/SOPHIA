"""Free-form chat adapters for the interactive chat REPL (scripts/chat.py)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Callable, Sequence

from sophia.runtime.execution_contract import ExecutionContract

from .completion import CompletionReport, reconcile_timeout
from .progress import WaitProgress
from .runner import Runner
from .stream_text import extract_chat_final_text, extract_cli_diagnostic
from .timing import ProgressStats, TimingLog, TurnTiming
from .usage import TokenUsage, UsageLog, UsageTracker, extract_token_usage


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
    usage_scope = "unknown"

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
        self.last_completion_report: CompletionReport | None = None
        self.last_usage: TokenUsage | None = None
        self.usage_total = TokenUsage()
        self.last_usage_observation = None
        self.last_usage_log_error: str | None = None
        self.last_context_input_tokens: int | None = None
        self._usage_tracker = UsageTracker()
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
        self.telemetry_reset_reason: str | None = None
        self._turn_id: str | None = None
        self._last_attempt_id: str | None = None

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
        self.telemetry_reset_reason = "model_change"
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

    def send(self, task_id: str, prompt: str, *, context: str | None = None,
             execution_contract: ExecutionContract | None = None) -> str:
        if os.environ.get("SOPHIA_G2_CHAT_RECOVERY") == "1":
            from .chat_recovery import recoverable_send
            return recoverable_send(self, task_id, prompt, context=context,
                                    execution_contract=execution_contract)
        return self._send(task_id, prompt, context=context, execution_contract=execution_contract)

    def _send(self, task_id: str, prompt: str, *, context: str | None = None,
              execution_contract: ExecutionContract | None = None) -> str:
        self._turn_id = uuid.uuid4().hex
        self._last_attempt_id = None
        # G2 C rollout is opt-in. A resume-failure retry inherits this send's
        # original deadline instead of receiving another full timeout.
        if execution_contract is not None:
            if execution_contract.surface != "live_chat" or execution_contract.task_id != task_id:
                raise ValueError("execution contract does not match this chat task")
            budget = execution_contract.next_attempt(uuid.uuid4().hex)
        else:
            budget = (ExecutionContract.start("live_chat", task_id, uuid.uuid4().hex,
                                              self.timeout_seconds)
                      if os.environ.get("SOPHIA_G2_DEADLINE_CONTRACT") == "1" else None)
        self.last_reset = False
        self.last_usage = None
        self.last_usage_observation = None
        # Neither installed CLI exposes a verified current-context metric here.
        # Consumption totals must never be used as a substitute.
        self.last_context_input_tokens = None
        self._binary = self._resolve_binary()
        was_resuming = self._session_id is not None
        self.last_completion_report = None
        try:
            code, stdout, stderr = self._run(task_id, self._command(), prompt, context=context,
                                             execution_contract=budget)
        except subprocess.TimeoutExpired as exc:
            report: CompletionReport | None = getattr(exc, "completion_report", None)
            if report is not None and report.recoverable:
                # The CLI finished its turn (terminal event received) and the process is
                # verifiably gone: only its exit hung. Keep the result, and keep the
                # timeout visible in the timing log (outcome stays "timeout").
                stdout = str(exc.output or "")
                for line in stdout.splitlines():
                    self._handle_event_line(line)
                return report.final_text
            detail = report.describe() if report is not None else "no completion evidence"
            raise ChatAdapterError(
                f"{self.executable} CLI timed out after {self.timeout_seconds:g}s: {detail}; "
                "verify the workspace before retrying") from exc
        with self._active_lock:
            was_cancelled = task_id in self._cancelled_task_ids
            self._cancelled_task_ids.discard(task_id)
        if was_cancelled:
            raise ChatAdapterInterrupted("request stopped to apply a new steering instruction")
        if code != 0 and was_resuming and self._is_resume_failure(stdout, stderr):
            # The replacement call has only the caller's delta prompt.  Do not
            # stream that context-incomplete reply; ChatSession will detect
            # last_reset and immediately resend a complete local context.
            self.telemetry_reset_reason = "resume_failure"
            self.reset_session()
            self.last_reset = True
            retry_budget = budget.next_attempt(uuid.uuid4().hex) if budget else None
            code, stdout, stderr = self._run(task_id, self._command(), prompt, context=context,
                                             display=False, execution_contract=retry_budget)
        if code != 0:
            diagnostic = extract_cli_diagnostic(stdout, stderr)
            raise ChatAdapterError(f"{self.executable} CLI exited with code {code}: {diagnostic}")
        for line in stdout.splitlines():
            self._handle_event_line(line)
        return extract_chat_final_text(stdout)

    def _record_usage(self, stdout: str, *, new_session: bool) -> None:
        for line in stdout.splitlines():
            self._handle_event_line(line)
        observation = self._usage_tracker.observe(extract_token_usage(stdout), scope=self.usage_scope,
                                                  session_id=self._session_id, new_session=new_session)
        self.last_usage_observation = observation
        self.last_usage = observation.incremental if observation is not None else None
        if self.last_usage is not None:
            self.usage_total = self.usage_total + self.last_usage
        self.last_usage_log_error = None
        try:
            self.usage_log.record(self.display_name, self.last_usage, observation=observation,
                                  turn_id=self._turn_id, attempt_id=self._last_attempt_id)
        except OSError as exc:
            # Logging failure is not evidence that the completed provider failed.
            self.last_usage_log_error = str(exc)[:800]

    def _run(self, task_id: str, command: Sequence[str], prompt: str, *, context: str | None = None,
             display: bool = True,
             execution_contract: ExecutionContract | None = None) -> tuple[int, str, str]:
        with self._active_lock:
            self._active_task_id = task_id
        suffix = f" ({context})" if context else ""
        progress = WaitProgress(f"{self.display_name} 응답 대기 중{suffix}", enabled=self.show_progress)
        progress.start()
        self._last_attempt_id = (execution_contract.attempt_id if execution_contract
                                 else uuid.uuid4().hex)
        timing = TurnTiming(label=self.display_name, start=time.monotonic(),
                            turn_id=self._turn_id, attempt_id=self._last_attempt_id,
                            session_mode="resumed" if self._session_id else "new",
                            reset_reason=self.telemetry_reset_reason)
        stats = ProgressStats(timing.start, streaming=self.on_line is not None)
        captured_stdout = None
        self.last_usage_log_error = None
        self.telemetry_reset_reason = None
        try:
            timeout = (execution_contract.require_remaining_s() if execution_contract
                       else self.timeout_seconds)
            runner_budget = {"deadline_at": execution_contract.deadline_at} if execution_contract else {}
            if self.on_line is not None:
                def on_line_then_stop(line: str) -> None:
                    timing.last_output = time.monotonic()
                    if timing.first_output is None:
                        timing.first_output = timing.last_output
                    try:
                        stats.observe(line, timing.last_output)
                    except Exception:
                        try:
                            stats.observe_unknown(line, timing.last_output)
                        except Exception:
                            pass  # observation must not alter response or lifecycle
                    # Parse metadata (resolved model, session id) as each line
                    # arrives, ahead of the on-completion pass in send(), so a
                    # live-streamed title reflects it on this same turn.
                    self._handle_event_line(line)
                    progress.stop()
                    if display:
                        self.on_line(line)

                result = self.runner.run_capture_streaming(
                    task_id, command, input_text=prompt, cwd=self.cwd, timeout=timeout,
                    on_line=on_line_then_stop, **runner_budget)
            else:
                result = self.runner.run_capture(
                    task_id, command, input_text=prompt, cwd=self.cwd, timeout=timeout,
                    **runner_budget)
                timing.first_output = timing.last_output = time.monotonic()
            captured_stdout = result[1]
            timing.exit_code = result[0]
            with self._active_lock:
                cancelled = task_id in self._cancelled_task_ids
            timing.outcome = "cancelled" if cancelled else ("completed" if result[0] == 0 else "nonzero_exit")
            self._record_usage(result[1], new_session=timing.session_mode == "new")
            return result
        except subprocess.TimeoutExpired as exc:
            captured_stdout = (exc.output.decode("utf-8", errors="replace") if isinstance(exc.output, bytes)
                               else str(exc.output or ""))
            timing.outcome = "timeout"
            report = reconcile_timeout(exc.output, exc.stderr, getattr(self.runner, "last_events", None))
            timing.reconciliation = report.state
            self.last_completion_report = report
            exc.completion_report = report  # type: ignore[attr-defined]
            self._record_usage(str(exc.output or ""), new_session=timing.session_mode == "new")
            raise
        except TimeoutError:
            timing.outcome = "timeout"
            raise
        except BaseException:
            timing.outcome = "error"
            self._usage_tracker.gap = True
            raise
        finally:
            timing.end = time.monotonic()
            timing.events = {name: at for name, at in (getattr(self.runner, "last_events", None) or {}).items()
                             if at >= timing.start}
            timing.event_details = {name: detail for name, detail in
                                    (getattr(self.runner, "last_event_details", None) or {}).items()
                                    if name in timing.events}
            timing.output_stats = (dict(getattr(self.runner, "last_output_stats", None) or {})
                                   if "process_start" in timing.events else {})
            try:
                timing.progress = stats.snapshot(timing.end, timing.events, timing.outcome, stdout=captured_stdout)
            except Exception:
                timing.progress = {"observation_errors": stats.observation_errors + 1,
                                   "kinds": {"unknown": max(1, stats.stdout_lines)}}
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
            and any(marker in diagnostic for marker in (
                "not found", "does not exist", "unknown", "invalid",
                # A conversation still held open by another writer is just as
                # unusable as a missing one: an earlier turn's CLI process
                # outlived its timeout and never released the store. Retrying the
                # same id only re-collides, so treat it as a dead identity and
                # start a fresh conversation (ChatSession resends local context).
                "active writer", "conflict", "already in use", "locked",
            ))
        )


class ClaudeChatAdapter(ChatAdapter):
    executable = "claude"
    display_name = "Claude"
    usage_scope = "turn"

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
    # Verified against this installation's turn.completed and local rollout.
    usage_scope = "session"

    def __init__(self, *args, sandbox: str = "workspace-write", model: str | None = None,
                 network_access: bool = False, **kwargs) -> None:
        super().__init__(*args, model=model, **kwargs)
        self.sandbox = sandbox
        self.network_access = network_access
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
        if self.sandbox == "workspace-write":
            enabled = str(self.network_access).lower()
            command += ["--config", f"sandbox_workspace_write.network_access={enabled}"]
        command += ["--skip-git-repo-check"]
        if self.model:
            command += ["--model", self.model]
        command += ["--json"]
        return command

    def set_network_access(self, enabled: bool) -> bool:
        """Change outbound access for future turns and discard the old sandbox session."""
        enabled = bool(enabled)
        if self.network_access == enabled:
            return False
        self.network_access = enabled
        self.reset_session()
        return True

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
