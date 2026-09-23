"""Turn-level latency measurement for CLI/chat adapters.

Blocking CLI turns (chat_adapter.py, cli_adapter.py) take anywhere from a few
seconds to a few minutes, and until now nothing recorded *where* that time
went -- process startup, the remote model actually thinking, or local
streaming/parsing. TurnTiming captures three wall-clock points (process
start, first output, completion) per call so the real bottleneck can be read
from a log instead of guessed at.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

# Below this gap, first output and completion are effectively simultaneous
# (e.g. a non-streaming call, where "first output" is just "the whole reply
# arrived") -- not worth reporting as a separate lead time.
_MIN_REPORTABLE_LEAD_SECONDS = 0.05


@dataclass
class TurnTiming:
    label: str
    start: float
    first_output: float | None = None
    end: float | None = None

    @property
    def to_first_output(self) -> float | None:
        return None if self.first_output is None else self.first_output - self.start

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
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def start_timing(label: str) -> TurnTiming:
    return TurnTiming(label=label, start=time.monotonic())
