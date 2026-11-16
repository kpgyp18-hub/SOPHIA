"""Post-timeout completion reconciliation (patch 03).

A result arriving, a result being persisted and the process exiting are three
independent facts.  When a turn hits its deadline the adapter used to know only
"timed out"; this module classifies what the partial output proves so the caller
can distinguish a hung-after-finish CLI from a turn that never produced a result.

It does not probe the workspace: side effects are always reported "unverified"
because partial stdout cannot prove them.  A completed event does prove the CLI
finished its own turn (tools included), which is what a recovered result relies on.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .stream_text import extract_chat_final_text

# Terminal events: Claude --output-format stream-json ends with type "result";
# Codex --json ends a turn with "turn.completed".
_FINAL_EVENT_TYPES = ("result", "turn.completed")

COMPLETED_BEFORE_TIMEOUT = "completed_before_timeout"
PARTIAL_OUTPUT = "partial_output"
NO_OUTPUT = "no_output"


@dataclass(frozen=True)
class CompletionReport:
    state: str                     # COMPLETED_BEFORE_TIMEOUT / PARTIAL_OUTPUT / NO_OUTPUT
    final_event_seen: bool
    final_text: str
    process_reaped: bool | None    # None = unknown (runner exposed no cleanup evidence)
    output_lines: int
    stderr_bytes: int
    side_effects: str = "unverified"

    @property
    def recoverable(self) -> bool:
        """A final event with text, from a process that is verifiably gone."""
        return (self.state == COMPLETED_BEFORE_TIMEOUT and bool(self.final_text)
                and self.process_reaped is True)

    def describe(self) -> str:
        reaped = {True: "process reaped", False: "process NOT confirmed exited", None: "process exit unknown"}[self.process_reaped]
        return (f"{self.state} ({self.output_lines} output lines, {self.stderr_bytes} stderr bytes, "
                f"{reaped}, side effects {self.side_effects})")


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def reconcile_timeout(stdout: object, stderr: object, events: dict[str, float] | None) -> CompletionReport:
    out, err = _as_text(stdout), _as_text(stderr)
    lines = [line for line in out.splitlines() if line.strip()]
    final_seen = False
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") in _FINAL_EVENT_TYPES:
            final_seen = True
    final_text = extract_chat_final_text(out) if final_seen else ""
    if final_seen:
        state = COMPLETED_BEFORE_TIMEOUT
    elif lines:
        state = PARTIAL_OUTPUT
    else:
        state = NO_OUTPUT
    reaped = None if events is None else ("cleanup_end" in events)
    return CompletionReport(state=state, final_event_seen=final_seen, final_text=final_text,
                            process_reaped=reaped, output_lines=len(lines),
                            stderr_bytes=len(err.encode("utf-8", errors="replace")))
