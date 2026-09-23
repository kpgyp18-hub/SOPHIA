"""Bounded, durable working context for sequential S.O.P.H.I.A tasks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sophia.context import ContextCompressor


class ContextManager:
    """Keep only a compact working set; never replay an unbounded transcript."""

    def __init__(self, path: str | Path = "workspace/sophia/context.json", *,
                 recent_turns: int = 6, max_recent_chars: int = 6_000,
                 max_summary_chars: int = 3_000, compressor: ContextCompressor | None = None,
                 session_id: str | None = None) -> None:
        self.path = Path(path)
        self.recent_turns = recent_turns
        self.max_recent_chars = max_recent_chars
        self.max_summary_chars = max_summary_chars
        self.compressor = compressor
        self.session_id = session_id

    def build(self) -> str:
        state = self._load()
        parts: list[str] = []
        if state["summary"]:
            parts.append("Compressed earlier context:\n" + state["summary"])
        if state["turns"]:
            turns = "\n".join(f"{turn['role']}: {turn['text']}" for turn in state["turns"])
            parts.append("Recent shared context:\n" + turns)
        return "\n\n".join(parts)

    def add(self, task_id: str, user_input: str, result: str, *, target_path: str | None = None,
            task_type: str | None = None) -> None:
        state = self._load()
        if target_path:
            state["last_file_target"] = target_path
        if task_type:
            state["last_task_type"] = task_type
        state["turns"].extend((
            {"task_id": task_id, "role": "User", "text": self._clean(user_input)},
            {"task_id": task_id, "role": "SOPHIA", "text": self._clean(result)},
        ))
        self._compact(state)
        self._save(state)
        self._save_summary(state)

    def last_file_target(self) -> str | None:
        """Return the most recently named workspace file, even after compaction."""
        target = self._load().get("last_file_target")
        return target if isinstance(target, str) and target else None

    def last_task_type(self) -> str | None:
        value = self._load().get("last_task_type")
        return value if isinstance(value, str) else None

    @staticmethod
    def _clean(text: str, limit: int = 900) -> str:
        normalized = " ".join(text.split())
        return normalized if len(normalized) <= limit else normalized[:limit - 1].rstrip() + "…"

    def _compact(self, state: dict[str, Any]) -> None:
        turns: list[dict[str, str]] = state["turns"]
        while len(turns) > self.recent_turns or self._turn_chars(turns) > self.max_recent_chars:
            oldest = turns.pop(0)
            line = f"{oldest['role']}: {oldest['text']}"
            state["summary"] = "\n".join(filter(None, (state["summary"], line)))
        while len(state["summary"]) > self.max_summary_chars and "\n" in state["summary"]:
            state["summary"] = state["summary"].split("\n", 1)[1]

    @staticmethod
    def _turn_chars(turns: list[dict[str, str]]) -> int:
        return sum(len(turn["role"]) + len(turn["text"]) + 2 for turn in turns)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._with_persisted_summary({"summary": "", "turns": [], "last_file_target": None, "last_task_type": None})
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._with_persisted_summary({"summary": "", "turns": [], "last_file_target": None, "last_task_type": None})
        return self._with_persisted_summary({
            "summary": str(state.get("summary", "")),
            "turns": list(state.get("turns", [])),
            "last_file_target": state.get("last_file_target"),
            "last_task_type": state.get("last_task_type"),
        })

    def _with_persisted_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.compressor is None or not self.session_id:
            return state
        try:
            persisted = self.compressor.load(self.session_id)
        except RuntimeError:
            return state
        if persisted is None:
            return state
        if persisted.summary:
            state["summary"] = persisted.summary
        for key in ("last_file_target", "last_task_type"):
            if state.get(key) is None and persisted.metadata.get(key) is not None:
                state[key] = persisted.metadata[key]
        return state

    def _save_summary(self, state: dict[str, Any]) -> None:
        if self.compressor is None or not self.session_id:
            return
        try:
            self.compressor.save(self.session_id, state["summary"], metadata={
                "last_file_target": state.get("last_file_target"),
                "last_task_type": state.get("last_task_type"),
                "recent_turn_count": len(state["turns"]),
            })
        except RuntimeError:
            # The JSON state remains the safe fallback when all DB backends fail.
            return

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
