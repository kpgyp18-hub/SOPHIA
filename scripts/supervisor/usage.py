"""Token-usage extraction and append-only records for chat CLI turns."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """Provider-reported token counts for one completed turn.

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
    return value if isinstance(value, int) and value >= 0 else 0


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


class UsageLog:
    """Append provider-reported per-turn usage to a JSONL file."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, label: str, usage: TokenUsage | None) -> None:
        if self.path is None or usage is None:
            return
        row = {"label": label, **usage.__dict__}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
