"""Raw consumption reports, explicit normalization and append-only chat records."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption counts, not a measure of current context length.

    Counts remain optional because a CLI/provider version may omit usage from
    its event stream.  The UI never estimates tokens or cost.
    """
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: object) -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    def summary(self) -> str:
        input_text = f"입력 {self.input_tokens:,}"
        if self.cached_input_tokens:
            input_text += f" (캐시 {self.cached_input_tokens:,})"
        output_text = f"출력 {self.output_tokens:,}"
        if self.reasoning_output_tokens:
            output_text += f" (추론 {self.reasoning_output_tokens:,})"
        return f"{input_text} · {output_text}"


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage_from_mapping(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens")
    if not any(key in value for key in keys):
        return None
    return TokenUsage(
        input_tokens=_count(value.get("input_tokens")),
        cached_input_tokens=_count(value.get("cached_input_tokens")) or _count(value.get("cache_read_input_tokens")),
        output_tokens=_count(value.get("output_tokens")),
        reasoning_output_tokens=_count(value.get("reasoning_output_tokens")),
    )


def extract_token_usage(stdout: str) -> TokenUsage | None:
    """Read the final provider usage object from Claude/Codex JSONL output."""
    found: TokenUsage | None = None
    for line in stdout.splitlines():
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        usage = _usage_from_mapping(event.get("usage"))
        if usage is not None:
            found = usage
    return found


@dataclass(frozen=True)
class UsageObservation:
    raw: TokenUsage
    incremental: TokenUsage | None
    scope: str
    attribution: str


class UsageTracker:
    """Normalize the installed Codex CLI's observed session-total counters.

    An unfamiliar resumed session or a decreasing counter establishes a new
    baseline only. Missing reports keep the last baseline; the next delta is
    explicitly attributed to the interval, including interrupted work.
    """
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.baseline: TokenUsage | None = None
        self.gap = False

    def observe(self, raw: TokenUsage | None, *, scope: str, session_id: str | None,
                new_session: bool) -> UsageObservation | None:
        if raw is None:
            if scope == "session" and session_id and (new_session or session_id != self.session_id):
                self.session_id = session_id
                self.baseline = TokenUsage() if new_session else None
            self.gap = True
            return None
        if scope == "turn":
            return UsageObservation(raw, raw, scope, "turn")
        if scope != "session" or not session_id:
            return UsageObservation(raw, None, "unknown", "unavailable")
        previous = self.baseline if session_id == self.session_id and not new_session else None
        self.session_id, self.baseline = session_id, raw
        gap, self.gap = self.gap, False
        if previous is None:
            return UsageObservation(raw, raw if new_session else None, scope,
                                    "since_session_start" if new_session else "baseline_only")
        names = [field.name for field in fields(TokenUsage)]
        if any(getattr(raw, name) < getattr(previous, name) for name in names):
            return UsageObservation(raw, None, scope, "counter_reset")
        delta = TokenUsage(**{name: getattr(raw, name) - getattr(previous, name) for name in names})
        return UsageObservation(raw, delta, scope, "since_last_report" if gap else "turn")


class UsageLog:
    """Append normalized consumption and its raw scope to a JSONL file."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, label: str, usage: TokenUsage | None, *,
               turn_id: str | None = None, attempt_id: str | None = None,
               observation: UsageObservation | None = None) -> None:
        if self.path is None or (usage is None and observation is None):
            return
        row = {"label": label}
        if usage is not None:
            row.update(usage.__dict__)
        if observation is not None:
            row.update(usage_schema_version=2, usage_scope=observation.scope, attribution=observation.attribution,
                       raw_usage=observation.raw.__dict__, incremental_available=usage is not None)
        if turn_id is not None:
            row["turn_id"] = turn_id
        if attempt_id is not None:
            row["attempt_id"] = attempt_id
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
