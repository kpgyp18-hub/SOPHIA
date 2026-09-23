"""Shared Runner-backed CLI adapter utilities."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .progress import WaitProgress
from .runner import Runner
from .schema import SchemaValidationError, normalize_structured_out_json, validate_out_json
from .timing import TimingLog, TurnTiming

class CLIAdapterError(RuntimeError):
    pass

class CLIAdapter:
    executable = ""

    def __init__(self, runner: Runner, *, timeout_seconds: float = 120.0, cwd: str | None = None,
                 executable_path: str | None = None, on_line: Callable[[str], None] | None = None,
                 show_progress: bool = True, timing_log: TimingLog | None = None) -> None:
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.executable_path = executable_path
        self.on_line = on_line
        self.show_progress = show_progress
        self.timing_log = timing_log or TimingLog(None)
        self.last_timing: TurnTiming | None = None
        self.last_stdout = ""
        self.last_stderr = ""

    def execute(self, task: Mapping[str, Any]) -> dict[str, Any]:
        command_path = self.executable_path
        if command_path is None:
            command_path = shutil.which(f"{self.executable}.cmd") if os.name == "nt" else None
            command_path = command_path or shutil.which(self.executable)
        if not command_path:
            raise CLIAdapterError(f"{self.executable} CLI not found")
        self._binary = command_path
        prompt = self._prompt(task)
        progress = WaitProgress(self._progress_description(task),
                                enabled=self.show_progress)
        progress.start()
        timing = TurnTiming(label=f"{task.get('agent', self.executable)} {task.get('step_id', '')}".strip(),
                            start=time.monotonic())
        try:
            if self.on_line is not None:
                def on_line_then_stop(line: str) -> None:
                    if timing.first_output is None:
                        timing.first_output = time.monotonic()
                    progress.stop()
                    self.on_line(line)

                code, stdout, stderr = self.runner.run_capture_streaming(
                    str(task["step_id"]), self._streaming_command(task), input_text=prompt,
                    cwd=self.cwd, timeout=self.timeout_seconds, on_line=on_line_then_stop)
            else:
                code, stdout, stderr = self.runner.run_capture(str(task["step_id"]), self._command(task), input_text=prompt,
                                                                cwd=self.cwd, timeout=self.timeout_seconds)
                timing.first_output = time.monotonic()
        finally:
            timing.end = time.monotonic()
            progress.stop()
            self.last_timing = timing
            self.timing_log.record(timing)
        self.last_stdout, self.last_stderr = stdout, stderr
        if code != 0:
            diagnostic = (stderr.strip() or stdout.strip())[-240:]
            raise CLIAdapterError(f"{self.executable} CLI exited with code {code}: {diagnostic}")
        payload = normalize_structured_out_json(self._extract_machine_output(stdout))
        try:
            validate_out_json(payload)
        except SchemaValidationError as exc:
            raise CLIAdapterError(f"CLI returned invalid machine output: {exc}") from exc
        return payload

    def _progress_description(self, task: Mapping[str, Any]) -> str:
        """Build a terminal-friendly description including known route progress."""
        agent = str(task.get("agent", self.executable))
        step_id = str(task.get("step_id", "")).strip()
        description = " ".join(part for part in (agent, step_id) if part)
        total = task.get("step_total")
        step = task.get("step_number")
        if step is None and step_id:
            try:
                step = int(step_id.split("-", 1)[0])
            except ValueError:
                pass
        if isinstance(step, int) and isinstance(total, int):
            description += f" ({step}/{total}단계)"
        return f"{description} 진행 중"

    def _command(self, task: Mapping[str, Any]) -> Sequence[str]:
        raise NotImplementedError

    def _streaming_command(self, task: Mapping[str, Any]) -> Sequence[str]:
        """Command variant for --watch mode; defaults to the non-streaming command.
        Adapters override this to add their CLI's live-event-stream flags."""
        return self._command(task)

    _HANDOFF_GUIDANCE = {
        "CLAUDE": ("If it genuinely requires code implementation, do not perform the implementation yourself: "
                   "return status OK with a handoff_request whose target_agent is CODEX and whose reason_code is "
                   "EXACTLY ONE of IMPLEMENTATION_REQUIRED, TEST_REQUIRED, DEBUG_REQUIRED, DATA_PROCESSING_REQUIRED "
                   "(these are the only reason_code values CLAUDE is allowed to send; never send "
                   "RISK_REVIEW_REQUIRED, DESIGN_REQUIRED, REQUIREMENT_CLARIFICATION_REQUIRED, or "
                   "SEMANTIC_REVIEW_REQUIRED — those may only be sent by CODEX). Use return_to_agent NONE unless "
                   "you explicitly need CODEX to hand control back, required_files [], required_skills [], and a "
                   "scope string that fully restates what the next agent must do, including any follow-up handoff "
                   "it must itself request. If this is a review step and you find no blocking issues, simply return "
                   "status OK with no handoff_request to finish the task; if you find issues that require code "
                   "changes, send a handoff back to CODEX using IMPLEMENTATION_REQUIRED or DEBUG_REQUIRED as above."),
        "CODEX": ("If it genuinely requires design judgment, risk/security review, requirement clarification, or "
                   "semantic review beyond implementation, do not perform that judgment yourself: return status OK "
                   "with a handoff_request whose target_agent is CLAUDE and whose reason_code is EXACTLY ONE of "
                   "RISK_REVIEW_REQUIRED, DESIGN_REQUIRED, REQUIREMENT_CLARIFICATION_REQUIRED, "
                   "SEMANTIC_REVIEW_REQUIRED (these are the only reason_code values CODEX is allowed to send; never "
                   "send IMPLEMENTATION_REQUIRED, TEST_REQUIRED, DEBUG_REQUIRED, or DATA_PROCESSING_REQUIRED — "
                   "those may only be sent by CLAUDE). Use return_to_agent NONE, required_files [], "
                   "required_skills [], and a scope string describing exactly what CLAUDE should review. Otherwise "
                   "implement the described work directly and return status OK."),
    }

    @staticmethod
    def _prompt(task: Mapping[str, Any]) -> str:
        payload = {key: task.get(key) for key in ("step_id", "role", "required_skills", "input_paths", "previous_step", "return_to")}
        description = task.get("description")
        base = ("Return only one strict JSON object matching the required machine output contract. "
                "No markdown, prose, Persona text, or presentation labels. Do not use tools and do not read, "
                "modify, or create files.")
        if description:
            guidance = CLIAdapter._HANDOFF_GUIDANCE.get(task.get("agent"), CLIAdapter._HANDOFF_GUIDANCE["CLAUDE"])
            instructions = (base + " A task description is provided below. If it can be answered directly without "
                             "further specialized work, do so and return status OK. " + guidance)
            return instructions + "\nTask:\n" + json.dumps(payload, ensure_ascii=False) + "\nDescription:\n" + description
        return (base + " For a task with no inputs, return status OK and the supplied step_id.\nTask:\n"
                + json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _extract_machine_output(text: str) -> dict[str, Any]:
        for candidate in reversed([line.strip() for line in text.splitlines() if line.strip()]):
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            found = CLIAdapter._find_machine_output(value)
            if found is not None:
                return found
        raise CLIAdapterError("CLI returned no valid machine output")

    @staticmethod
    def _find_machine_output(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict) and {"status", "step_id"}.issubset(value):
            return value
        if not isinstance(value, dict):
            return None
        # Codex --json event log: {"type":"item.completed","item":{"type":"agent_message","text":"{...}"}}
        item = value.get("item")
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            found = CLIAdapter._parse_nested(item["text"])
            if found is not None:
                return found
        for key in ("structured_output", "result", "output"):
            nested = value.get(key)
            if isinstance(nested, dict) and {"status", "step_id"}.issubset(nested):
                return nested
            if isinstance(nested, str):
                found = CLIAdapter._parse_nested(nested)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _parse_nested(text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and {"status", "step_id"}.issubset(parsed):
            return parsed
        return None
