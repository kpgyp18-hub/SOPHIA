"""Turn-level latency measurement for CLI/chat adapters.

Blocking CLI turns (chat_adapter.py, cli_adapter.py) take anywhere from a few
seconds to a few minutes, and until now nothing recorded *where* that time
went -- process startup, the remote model actually thinking, or local
streaming/parsing. TurnTiming captures three wall-clock points (process
start, first output, completion) per call so the real bottleneck can be read
from a log instead of guessed at.
"""
from __future__ import annotations

import io
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .stream_text import classify_progress

# Below this gap, first output and completion are effectively simultaneous
# (e.g. a non-streaming call, where "first output" is just "the whole reply
# arrived") -- not worth reporting as a separate lead time.
_MIN_REPORTABLE_LEAD_SECONDS = 0.05
_PROGRESS_KINDS = ("text", "tool_start", "tool_end", "terminal", "meta", "unknown")


@dataclass(slots=True)
class ProgressStats:
    """Bounded counters, not a transcript. At most 64 tool ids of 256 characters.

    Times are adapter callback times. Non-streaming captures provide counts only.
    Silence in Codex JSON can include message generation, so phases describe
    observable stdout state at the deadline, never the cause of a stall.
    """
    start: float
    streaming: bool = True
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_PROGRESS_KINDS, 0))
    stdout_lines: int = 0
    stdout_bytes: int = 0
    observation_errors: int = 0
    tool_count: int = 0
    tool_overflow: int = 0
    unmatched_tool_ends: int = 0
    tool_time: float = 0.0
    last_kind: str | None = None
    last_progress_kind: str | None = None
    first_progress: float | None = None
    last_progress: float | None = None
    last_line: float | None = None
    longest_gap: float = 0.0
    longest_gap_start: float | None = None
    longest_progress_gap: float = 0.0
    longest_progress_gap_start: float | None = None
    terminal_seen: bool = False
    _tools: dict[str, float | None] = field(default_factory=dict)

    def observe(self, line: str, now: float | None = None) -> None:
        try:
            kind = classify_progress(line)
            if kind not in self.counts:
                kind = "unknown"
            tool_id = None
            if kind in ("tool_start", "tool_end"):
                tool_id = json.loads(line).get("item", {}).get("id")
                if not isinstance(tool_id, str) or not 0 < len(tool_id) <= 256:
                    tool_id = None
        except Exception:
            self.observation_errors += 1
            kind, tool_id = "unknown", None
        self._record(line, kind, tool_id, now)

    def observe_unknown(self, line: str, now: float | None = None) -> None:
        """Fallback if an observer implementation itself fails before recording."""
        self.observation_errors += 1
        self._record(line, "unknown", None, now)

    def _record(self, line, kind, tool_id, now):
        self.stdout_lines += 1
        self.stdout_bytes += len(line.encode("utf-8", errors="replace"))
        self.counts[kind] += 1
        self.last_kind = kind
        stamp = (float(now) if self.streaming and type(now) in (int, float)
                 and math.isfinite(now) and now >= self.start else None)
        if stamp is not None:
            previous = self.last_line if self.last_line is not None else self.start
            if stamp >= previous:
                if stamp - previous > self.longest_gap:
                    self.longest_gap, self.longest_gap_start = stamp - previous, previous
                self.last_line = stamp
        if kind not in ("meta", "unknown"):
            self.last_progress_kind = kind
            if stamp is not None:
                previous = self.last_progress if self.last_progress is not None else self.start
                if stamp >= previous:
                    if stamp - previous > self.longest_progress_gap:
                        self.longest_progress_gap, self.longest_progress_gap_start = stamp - previous, previous
                    self.last_progress = stamp
                    if self.first_progress is None:
                        self.first_progress = stamp
        if kind == "terminal":
            self.terminal_seen = True
        elif kind == "tool_start":
            self.tool_count += 1
            if tool_id is None or (tool_id not in self._tools and len(self._tools) >= 64):
                self.tool_overflow += 1
            elif tool_id not in self._tools:
                self._tools[tool_id] = stamp
        elif kind == "tool_end":
            if tool_id is not None and tool_id in self._tools:
                began = self._tools.pop(tool_id)
                if stamp is not None and began is not None:
                    self.tool_time += max(0.0, stamp - began)
            else:
                self.unmatched_tool_ends += 1

    def snapshot(self, end: float, events: dict, outcome: str | None, *, stdout: str | None = None) -> dict:
        if not self.streaming and stdout is not None:
            captured = ProgressStats(self.start, streaming=False)
            for line in io.StringIO(stdout):
                line = line.rstrip("\r\n")
                try:
                    captured.observe(line)
                except Exception:
                    captured.observe_unknown(line)
            result = captured.snapshot(end, events, outcome)
            result.update(stdout_bytes=len(stdout.encode("utf-8", errors="replace")),
                          byte_scope="utf8_reencoded_capture")
            return result
        # Final capture includes line endings and queued lines not delivered on
        # interruption. Counts classify delivered lines only; no fake timestamps.
        lines, size = self.stdout_lines, self.stdout_bytes
        if stdout is not None:
            lines = stdout.count("\n") + int(bool(stdout) and not stdout.endswith("\n"))
            size = len(stdout.encode("utf-8", errors="replace"))
        timed = self.streaming and "process_start" in events
        cutoff = min(end, events.get("timeout", end))
        gap, gap_start = self.longest_gap, self.longest_gap_start
        progress_gap, progress_start = self.longest_progress_gap, self.longest_progress_gap_start
        if timed:
            previous = self.last_line if self.last_line is not None else self.start
            if cutoff - previous > gap:
                gap, gap_start = cutoff - previous, previous
            previous = self.last_progress if self.last_progress is not None else self.start
            if cutoff - previous > progress_gap:
                progress_gap, progress_start = cutoff - previous, previous

        def relative(at):
            return round(at - self.start, 6) if timed and at is not None else None

        def duration(value):
            return round(value, 6) if timed else None

        result = {
            "schema_version": 1, "streaming": self.streaming, "kinds": dict(self.counts),
            "stdout_lines": lines, "stdout_bytes": size, "classified_lines": sum(self.counts.values()),
            "byte_scope": "utf8_reencoded_capture" if stdout is not None else "utf8_callback_payload",
            "observation_errors": self.observation_errors, "tool_count": self.tool_count,
            "tool_time_s": duration(self.tool_time), "tools_open_at_end": len(self._tools),
            "tool_overflow": self.tool_overflow, "unmatched_tool_ends": self.unmatched_tool_ends,
            "first_progress_s": relative(self.first_progress), "last_progress_s": relative(self.last_progress),
            "last_line_s": relative(self.last_line), "last_event_kind": self.last_kind,
            "last_progress_kind": self.last_progress_kind, "terminal_seen": self.terminal_seen,
            "tail_idle_s": duration(max(0.0, cutoff - self.last_line)) if self.last_line is not None else None,
            "longest_gap_s": duration(gap), "longest_gap_start_s": relative(gap_start),
            "longest_progress_gap_s": duration(progress_gap), "longest_progress_gap_start_s": relative(progress_start),
            "stdin_closed_s": (round(events["stdin_closed"] - self.start, 6) if "stdin_closed" in events else None),
        }
        result["timeout_phase"] = timeout_phase(events, result, outcome)
        return result


def timeout_phase(events: dict, progress: dict, outcome: str | None) -> str | None:
    """Observed phase only; missing stdin_closed means closure was not observed.

    A silent completed-message JSON stream can still be generating text. The
    5-second boundary is descriptive and tail_idle_s preserves the raw value.
    """
    if outcome != "timeout":
        return None
    if "process_start" not in events:
        return "before_start"
    if "stdin_closed" not in events:
        return "input"
    if progress["terminal_seen"] and "child_exit" not in events:
        return "terminal_seen_exit_hung"
    if not progress["stdout_lines"]:
        return "no_output"
    if progress["tools_open_at_end"]:
        return "tool_running"
    idle = progress["tail_idle_s"]
    if idle is None:
        return None  # capture has no per-line clock; do not guess a silence phase
    return "silent_after_progress" if idle >= 5.0 else "active_until_deadline"


@dataclass
class TurnTiming:
    label: str
    start: float
    first_output: float | None = None
    end: float | None = None
    # Patch 02 (P0-B): fields that let a timeout be told apart from a clean
    # finish. ``outcome`` stays None until the adapter classifies the turn
    # (completed / nonzero_exit / timeout / cancelled / error).
    last_output: float | None = None
    outcome: str | None = None
    exit_code: int | None = None
    # Patch 03: what a timed-out turn's partial output proved (completion.py state), else None.
    reconciliation: str | None = None
    # Runner lifecycle events (monotonic, same clock as ``start``); see Runner.last_events.
    events: dict[str, float] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="milliseconds"))
    turn_id: str | None = None
    attempt_id: str | None = None
    session_mode: str | None = None
    reset_reason: str | None = None
    event_details: dict[str, dict[str, str]] = field(default_factory=dict)
    output_stats: dict[str, int] = field(default_factory=dict)
    progress: dict = field(default_factory=dict)

    @property
    def to_first_output(self) -> float | None:
        return None if self.first_output is None else self.first_output - self.start

    @property
    def last_output_s(self) -> float | None:
        """Seconds from start to the most recent output (silence before the end = end - this)."""
        return None if self.last_output is None else self.last_output - self.start

    @property
    def total(self) -> float | None:
        return None if self.end is None else self.end - self.start

    def summary(self) -> str:
        total = self.total
        if total is None:
            return f"{self.label}: incomplete"
        lead = self.to_first_output
        if lead is not None and (total - lead) > _MIN_REPORTABLE_LEAD_SECONDS:
            return f"{self.label} {total:.1f}s (첫 출력 {lead:.1f}s)"
        return f"{self.label} {total:.1f}s"


class TimingLog:
    """Appends one JSON line per completed turn to ``path``.

    A ``None`` path makes ``record`` a no-op, so adapters can always hold a
    TimingLog instance without a separate "is logging enabled" branch.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, timing: TurnTiming) -> None:
        if self.path is None:
            return
        row = {
            "label": timing.label,
            "to_first_output_s": timing.to_first_output,
            "total_s": timing.total,
            "outcome": timing.outcome,
            "exit_code": timing.exit_code,
            "reconciliation": timing.reconciliation,
            "last_output_s": timing.last_output_s,
            "started_at": timing.started_at,
            "turn_id": timing.turn_id,
            "attempt_id": timing.attempt_id,
            "session_mode": timing.session_mode,
            "reset_reason": timing.reset_reason,
            "events_s": {k: round(v - timing.start, 3) for k, v in timing.events.items()},
            "event_details": timing.event_details,
            "output_stats": timing.output_stats,
            "progress": timing.progress,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def start_timing(label: str) -> TurnTiming:
    return TurnTiming(label=label, start=time.monotonic())
