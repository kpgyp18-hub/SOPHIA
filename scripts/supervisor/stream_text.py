"""Shared parsing for live CLI event streams (Claude --output-format stream-json /
Codex --json). Extracted from scripts/agents.py's --watch mode so the interactive
chat session (chat_session.py) can reuse the exact same, already-verified parsing
instead of duplicating it."""
from __future__ import annotations

import json
from typing import Any, NamedTuple


def classify_progress(line: str) -> str:
    """Classify only observed CLI shapes; unfamiliar tool protocols stay unknown.

    Claude tool-use shapes have no verified local sample yet. Codex JSON emits
    completed messages, not token deltas: no event does not imply no model work.
    """
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return "unknown"
    if not isinstance(event, dict):
        return "unknown"
    kind = event.get("type")
    if kind in ("thread.started", "turn.started", "system", "init"):
        return "meta"
    if kind in ("turn.completed", "result"):
        return "terminal"
    item = event.get("item")
    if isinstance(item, dict):
        if item.get("type") == "command_execution":
            if kind == "item.started":
                return "tool_start"
            if kind == "item.completed":
                return "tool_end"
        if kind == "item.completed" and item.get("type") == "agent_message":
            return "text"
    if kind == "stream_event":
        inner = event.get("event")
        delta = inner.get("delta") if isinstance(inner, dict) else None
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            return "text"
    return "unknown"


def extract_cli_diagnostic(stdout: str, stderr: str, *, limit: int = 800) -> str:
    """Prefer explicit CLI errors to stderr banners or trailing JSON metadata.

    Only error messages/codes are selected from structured output. Unrelated
    session, usage and credential metadata must not become the diagnostic.
    """
    diagnostic: str | None = None
    plain_lines: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            plain_lines.append(line)
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        parts: list[object] = []
        if kind in ("error", "turn.failed"):
            error = event.get("error")
            if isinstance(error, dict):
                parts = [error.get("code"), error.get("message")]
            elif isinstance(error, str):
                parts = [error]
            else:
                parts = [event.get("message")]
        elif kind == "result" and (event.get("is_error") is True
                                   or event.get("api_error")
                                   or str(event.get("subtype", "")).startswith("error")):
            parts = [event.get("api_error"), event.get("result")]
            errors = event.get("errors")
            if isinstance(errors, list):
                parts.extend(error if isinstance(error, str) else error.get("message")
                             for error in errors if isinstance(error, (str, dict)))
        message = ": ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        if message:
            diagnostic = message
    if diagnostic:
        return diagnostic[:limit]
    # Keep the existing plain-text stderr preference for non-JSON CLI errors.
    return (stderr.strip() or "\n".join(plain_lines).strip() or "no error diagnostic")[-limit:]


def extract_display_text(line: str) -> str | None:
    """Pull only human-readable message content out of one raw streaming JSONL event
    line. Returns None for plumbing/metadata events (session hooks, thread/turn
    lifecycle, usage, rate limits, duplicate full-message echoes of already-streamed
    deltas) so callers can show a clean transcript instead of the full event
    firehose."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    event_type = obj.get("type")
    if event_type == "stream_event":
        event = obj.get("event")
        delta = event.get("delta") if isinstance(event, dict) else None
        if isinstance(delta, dict):
            for key in ("text", "thinking", "partial_json"):
                value = delta.get(key)
                if isinstance(value, str) and value:
                    return value
        return None
    if event_type == "item.completed":
        item = obj.get("item")
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"]:
            return item["text"]
        return None
    return None


def extract_final_text(stdout: str) -> str:
    """Reconstructs the agent's final message text from a captured stream of JSONL
    event lines, preferring an explicit final-result event and falling back to
    concatenating every recognized display-text fragment (see extract_display_text)."""
    fragments: list[str] = []
    last_result: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result" and isinstance(obj.get("result"), str):
            last_result = obj["result"]
        text = extract_display_text(line)
        if text:
            fragments.append(text)
    if last_result is not None:
        return last_result.strip()
    return "".join(fragments).strip()


class ChatFragment(NamedTuple):
    """One piece of chat-visible text from a streaming event. is_block is True for
    a discrete, already-complete message (e.g. Codex's item.completed agent_message,
    which may itself be one of several separate messages in a turn) and False for an
    incremental token delta (Claude's stream_event text deltas) that continues the
    line in progress. Callers use is_block to decide whether to start a new line."""
    text: str
    is_block: bool


def extract_chat_display_text(line: str) -> ChatFragment | None:
    """Like extract_display_text, but for agents with real tool access (chat mode):
    excludes partial_json content-block deltas and non-message item types, since
    those carry tool-call arguments/results (e.g. a Write tool's file_path/content
    JSON) rather than anything the agent is actually saying to the chat. Without
    this, an agent that edits a file mid-reply would have that tool call's raw JSON
    arguments spliced into its visible chat message."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    event_type = obj.get("type")
    if event_type == "stream_event":
        event = obj.get("event")
        delta = event.get("delta") if isinstance(event, dict) else None
        if isinstance(delta, dict):
            value = delta.get("text")
            if isinstance(value, str) and value:
                return ChatFragment(value, is_block=False)
        return None
    if event_type == "item.completed":
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str) and item["text"]:
            return ChatFragment(item["text"], is_block=True)
        return None
    return None


def extract_chat_final_text(stdout: str) -> str:
    """extract_final_text's counterpart for extract_chat_display_text (see above);
    used to reconstruct the logged/recorded reply for a chat-mode turn. Separate
    message blocks (e.g. two distinct Codex agent_message items in one turn) are
    joined with a newline; delta fragments within one block are concatenated
    directly, same as the live streamed display."""
    fragments: list[str] = []
    last_result: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result" and isinstance(obj.get("result"), str):
            last_result = obj["result"]
        fragment = extract_chat_display_text(line)
        if fragment is None:
            continue
        if fragment.is_block and fragments:
            fragments.append("\n")
        fragments.append(fragment.text)
    if last_result is not None:
        return last_result.strip()
    return "".join(fragments).strip()
