"""Interactive shared chat session with resumable, delta-based agent prompts."""
from __future__ import annotations

import sys
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .usage import TokenUsage
from .chat_adapter import ChatAdapterInterrupted, format_resolved_model

_MENTION_TARGETS = {
    "claude": "Claude", "클로드": "Claude", "blanc": "Claude", "블랑": "Claude",
    "codex": "Codex", "코덱스": "Codex", "ade": "Codex", "에이드": "Codex",
    "both": "Both", "all": "Both", "둘다": "Both", "둘": "Both",
}
_ADDRESS_TARGETS = {
    "claude": "Claude", "클로드": "Claude", "blanc": "Claude", "블랑": "Claude",
    "codex": "Codex", "코덱스": "Codex", "ade": "Codex", "에이드": "Codex",
}
_ADDRESS_RE = re.compile(
    r"^\*{0,2}\s*(codex|코덱스|ade|에이드|claude|클로드|blanc|블랑)\s*(에게|한테)\*{0,2}\s*[:：]",
    re.IGNORECASE | re.MULTILINE,
)
_MAX_AUTO_RELAY_HOPS = 4
_MAX_HISTORY_TURNS = 40
_MAX_HISTORY_CHARS = 12000
_MAX_RECENT_TURNS = 32
_MAX_RECENT_CHARS = 12000
_MAX_SUMMARY_CHARS = 6000
# Remote CLI adapters keep their own resumed conversations.  Periodically
# discard that remote state after local compaction so the bounded local summary
# is the only context rehydrated on the next turn.
_COMPRESSION_RESET_EVICTIONS = 8
_PERSONA_CONTEXT = {
    "Claude": ("블랑", "personas/blanc_persona.txt"),
    "Codex": ("에이드", "personas/ade_persona.txt"),
}
_HELP_TEXT = (
    "commands: /default both|claude|codex (set who plain messages go to), "
    "/relay on|off (toggle automatic agent-to-agent relay; default: on), "
    "/timing on|off (toggle the per-reply latency line; default: on), "
    "/usage [on|off] (show session token totals or toggle per-reply usage; default: on), "
    "/model [claude|codex|both] [model-id|default] (show or change model; resets that remote session), "
    "/attach <path> (attach a workspace file to the next message), /attachments (list pending files), "
    "/attach clear (discard pending files), "
    "/run <message> (send an explicit task to the current default agent), "
    "/steer <instruction> (while a request is running, stop and restart it with this instruction), "
    "/clear (wipe local transcript and remote sessions), /exit or /quit, /help\n"
    "mention @claude/@블랑, @codex/@에이드, or @both to address a specific agent for one message. "
    "While an agent is working, new lines are accepted and queued in order. "
    "Replies beginning with 'Codex/에이드에게:' or 'Claude/블랑에게:' are relayed automatically."
)


@dataclass
class Turn:
    speaker: str
    text: str
    at: datetime = field(default_factory=datetime.now)


class ChatAgent(Protocol):
    display_name: str

    def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str: ...


def parse_input(raw: str, default_agent: str) -> tuple[str | None, str]:
    stripped = raw.strip()
    if stripped.startswith("/"):
        return None, stripped
    if stripped.startswith("@"):
        head, _, rest = stripped[1:].partition(" ")
        target = _MENTION_TARGETS.get(head.lower())
        if target:
            return target, rest.strip()
    return default_agent, stripped


def _addressed_agent(text: str) -> str | None:
    """Return the explicitly addressed agent at the start of a reply, if any."""
    match = _ADDRESS_RE.search(text)
    if match is None:
        return None
    return _ADDRESS_TARGETS[match.group(1).lower()]


def format_history_block(turns: list[Turn]) -> str:
    """Render a bounded set of turns; mainly useful for a newly contacted agent."""
    recent = turns[-_MAX_HISTORY_TURNS:]
    lines = [f"{turn.speaker}: {turn.text}" for turn in recent]
    block = "\n".join(lines)
    while len(block) > _MAX_HISTORY_CHARS and len(lines) > 1:
        lines.pop(0)
        block = "\n".join(lines)
    return block


def _condense_turn(turn: Turn, *, limit: int = 480) -> str:
    text = " ".join(turn.text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return f"{turn.speaker}: {text}"


def build_prompt(target: str, other_agent: str, history: list[Turn], new_message: str = "",
                 summary: str = "") -> str:
    """Build one incremental update for a persistent CLI conversation.

    ``history`` must contain only turns the target has not already received.  The
    optional summary is used only before an agent's first local send.
    """
    persona_name, persona_path = _PERSONA_CONTEXT.get(target, (target, ""))
    other_persona_name, _ = _PERSONA_CONTEXT.get(other_agent, (other_agent, ""))
    preamble = (
        f"You are {target} (S.O.P.H.I.A persona: {persona_name}), participating in a live "
        f"three-way group chat with the user and {other_agent} ({other_persona_name}, another "
        "AI CLI agent) in this same working directory. "
        "Reply conversationally and concisely, like a chat message, not a formal report. "
        "You have real file and shell tool access to this working directory; use it "
        "whenever the user (or the other agent's message, if referenced below) asks for "
        f"actual work, not just discussion. For a work handoff, review request, or result "
        f"that needs a response from {other_agent}, begin a separate line exactly with "
        f"'{other_agent}에게:' (or that agent's persona alias followed by '에게:'). "
        "This starts the bounded automatic relay; merely mentioning the other agent does not."
    )
    if persona_path:
        preamble += (
            f" Apply `{persona_path}` and `personas/orchestration_relationship.txt` throughout "
            "this group chat. Except in Critical Mode, retain an appropriate short persona tone "
            "in every reply; keep task facts, safety, and verification clear and dominant."
        )
    parts = [preamble]
    if summary:
        parts.append("Earlier conversation, condensed (details may be abbreviated):\n" + summary)
    delta = format_history_block(history)
    if delta:
        parts.append("New shared-chat messages since your last reply:\n" + delta)
    if new_message:
        parts.append(f"You: {new_message}")
    return "\n\n".join(parts)


class StreamPrinter:
    """Render native stream fragments, with optional TTY-only Codex pacing.

    Codex's JSON protocol reports completed agent messages rather than token
    deltas.  ``typewriter_speakers`` affects presentation only: callers still
    receive, store, and log the original complete reply.
    """

    def __init__(self, extract: Callable[[str], object], *,
                 typewriter_speakers: set[str] | None = None,
                 typing_delay: float = 0.02,
                 stream_prefix: Callable[[str], None] | None = None) -> None:
        self._extract = extract
        self._mid_line = False
        self._current: str | None = None
        # Claude and Codex stream from separate worker threads for @both.
        # Keep each rendered fragment intact so their labels and text cannot
        # be interleaved on the terminal.
        self._lock = threading.RLock()
        self._typewriter_speakers = typewriter_speakers or set()
        self._typing_delay = typing_delay
        self._stream_prefix = stream_prefix

    def on_line(self, speaker: str, line: str) -> None:
        fragment = self._extract(line)
        if not fragment:
            return
        with self._lock:
            text, is_block = fragment
            if self._mid_line and (self._current != speaker or is_block):
                print()
                self._mid_line = False
            if not self._mid_line:
                if self._stream_prefix is not None:
                    self._stream_prefix(speaker)
                else:
                    print(f"{speaker}: ", end="", flush=True)
            if is_block and speaker in self._typewriter_speakers:
                self._print_typewriter(text)
            else:
                print(text, end="", flush=True)
            self._current, self._mid_line = speaker, True

    def _print_typewriter(self, text: str) -> None:
        """Pace a completed message as word-sized display fragments."""
        chunks = re.findall(r"\S+\s*", text)
        for index, chunk in enumerate(chunks):
            print(chunk, end="", flush=True)
            if index + 1 < len(chunks):
                time.sleep(self._typing_delay)

    def flush(self) -> None:
        with self._lock:
            if self._mid_line:
                print()
                self._mid_line = False


class ChatSession:
    def __init__(self, agents: dict[str, ChatAgent], *, default_agent: str = "Claude",
                 log_path: Path | None = None, printer: Callable[[str, str], None] | None = None,
                 stream_printer: StreamPrinter | None = None, auto_relay_enabled: bool = True,
                 show_timing: bool = True, show_usage: bool = True,
                 attachment_root: Path | None = None, task_tracker: object | None = None) -> None:
        if default_agent not in agents and default_agent != "Both":
            raise ValueError(f"default_agent {default_agent!r} not in agents {sorted(agents)}")
        self.agents = agents
        self.default_agent = default_agent
        self.history: list[Turn] = []
        self.history_summary = ""
        self._last_sent_index = {name: 0 for name in agents}
        self._evictions_since_remote_reset = 0
        self.log_path = log_path
        self.printer = printer or self._console_printer
        self.stream_printer = stream_printer
        self.auto_relay_enabled = auto_relay_enabled
        self.show_timing = show_timing
        self.show_usage = show_usage
        self.usage_totals = {name: TokenUsage() for name in agents}
        self._usage_available: set[str] = set()
        self.attachment_root = (attachment_root or Path.cwd()).resolve()
        self._pending_attachments: list[Path] = []
        self.task_tracker = task_tracker
        self._active_task_id: str | None = None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _console_printer(speaker: str, text: str) -> None:
        print(f"{speaker}: {text}")

    def _record(self, speaker: str, text: str) -> None:
        turn = Turn(speaker=speaker, text=text)
        self.history.append(turn)
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as handle:
                prefix = f"[{turn.at:%H:%M:%S}] {speaker}: "
                body = ("\n" + " " * len(prefix)).join(text.split("\n"))
                handle.write(prefix + body + "\n")

    def _compact_history(self) -> None:
        evicted_count = 0
        while len(self.history) > _MAX_RECENT_TURNS or (
            len(format_history_block(self.history)) > _MAX_RECENT_CHARS and len(self.history) > 1
        ):
            evicted = self.history.pop(0)
            evicted_count += 1
            self.history_summary = "\n".join(filter(None, (self.history_summary, _condense_turn(evicted))))
            self._last_sent_index = {name: max(0, index - 1) for name, index in self._last_sent_index.items()}
            while len(self.history_summary) > _MAX_SUMMARY_CHARS and "\n" in self.history_summary:
                self.history_summary = self.history_summary.split("\n", 1)[1]
        self._evictions_since_remote_reset += evicted_count
        if self._evictions_since_remote_reset >= _COMPRESSION_RESET_EVICTIONS:
            self._reset_remote_sessions_for_compression()

    def _reset_remote_sessions_for_compression(self) -> None:
        """Rehydrate resumed CLIs from the bounded local summary next turn."""
        self._last_sent_index = {name: 0 for name in self.agents}
        for agent in self.agents.values():
            reset = getattr(agent, "reset_session", None)
            if callable(reset):
                reset()
        self._evictions_since_remote_reset = 0

    def handle_command(self, command: str) -> bool:
        name, _, rest = command[1:].partition(" ")
        name = name.lower()
        if name in ("exit", "quit"):
            return False
        if name == "help":
            self.printer("System", _HELP_TEXT)
        elif name == "default":
            target = _MENTION_TARGETS.get(rest.strip().lower())
            if target in ("Claude", "Codex", "Both"):
                self.default_agent = target
                self.printer("System", f"default agent set to {target}")
            else:
                self.printer("System", "usage: /default both|all|claude|blanc|codex|ade")
        elif name == "clear":
            self.history.clear()
            self.history_summary = ""
            self._last_sent_index = {agent: 0 for agent in self.agents}
            self._evictions_since_remote_reset = 0
            for agent in self.agents.values():
                reset = getattr(agent, "reset_session", None)
                if callable(reset):
                    reset()
            self.printer("System", "local transcript and remote sessions cleared (log file is untouched)")
        elif name == "run":
            if not rest.strip():
                self.printer("System", "usage: /run <message>")
            else:
                # /run is intentionally just an explicit dispatch alias: it
                # does not create a shell execution path or bypass the normal
                # task tracker, permissions, or agent context.
                message = self._take_attachments(rest.strip())
                self._record("You", message)
                self._compact_history()
                self._begin_task(message)
                return self._dispatch(self.default_agent)
        elif name == "relay":
            enabled = rest.strip().lower()
            if enabled in ("on", "off"):
                self.auto_relay_enabled = enabled == "on"
                self.printer("System", f"automatic relay {'enabled' if self.auto_relay_enabled else 'disabled'}")
            else:
                self.printer("System", "usage: /relay on|off")
        elif name == "timing":
            enabled = rest.strip().lower()
            if enabled in ("on", "off"):
                self.show_timing = enabled == "on"
                self.printer("System", f"latency line {'enabled' if self.show_timing else 'disabled'}")
            else:
                self.printer("System", "usage: /timing on|off")
        elif name == "usage":
            enabled = rest.strip().lower()
            if enabled in ("on", "off"):
                self.show_usage = enabled == "on"
                self.printer("System", f"per-reply token usage {'enabled' if self.show_usage else 'disabled'}")
            elif not enabled:
                if not self._usage_available:
                    self.printer("System", "session token usage unavailable (provider did not return usage)")
                else:
                    available_agents = [agent for agent in self.agents if agent in self._usage_available]
                    details = [f"{agent} {self.usage_totals[agent].summary()}" for agent in available_agents]
                    total = sum((self.usage_totals[agent] for agent in available_agents), TokenUsage())
                    self.printer("System", f"session token usage — {'; '.join(details)} | 합계 {total.summary()}")
            else:
                self.printer("System", "usage: /usage [on|off]")
        elif name == "model":
            self._handle_model_command(rest)
        elif name == "attach":
            self._handle_attach_command(rest)
        elif name in ("attachments", "files"):
            self._show_pending_attachments()
        elif name == "steer":
            self.printer("System", "steering is available while an interactive request is running")
        else:
            self.printer("System", f"unknown command: /{name} (try /help)")
        return True

    def _handle_attach_command(self, rest: str) -> None:
        supplied = rest.strip()
        if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in ("\"", "'"):
            supplied = supplied[1:-1]
        if supplied.lower() in ("clear", "cancel"):
            count = len(self._pending_attachments)
            self._pending_attachments.clear()
            self.printer("System", f"discarded {count} pending attachment{'s' if count != 1 else ''}")
            return
        if not supplied:
            self.printer("System", "usage: /attach <workspace-relative file path> | /attach clear")
            return
        candidate = Path(supplied)
        try:
            resolved = candidate.resolve() if candidate.is_absolute() else (self.attachment_root / candidate).resolve()
            resolved.relative_to(self.attachment_root)
        except (OSError, ValueError):
            self.printer("System", "attachment rejected: path must stay inside the chat workspace")
            return
        if not resolved.is_file():
            self.printer("System", "attachment rejected: file not found or not a regular file")
            return
        if resolved in self._pending_attachments:
            self.printer("System", f"attachment already pending: {resolved.relative_to(self.attachment_root)}")
            return
        self._pending_attachments.append(resolved)
        self.printer("System", f"attachment queued: {resolved.relative_to(self.attachment_root)}")

    def _show_pending_attachments(self) -> None:
        if not self._pending_attachments:
            self.printer("System", "no pending attachments")
            return
        names = ", ".join(str(path.relative_to(self.attachment_root)) for path in self._pending_attachments)
        self.printer("System", f"pending attachments (next message): {names}")

    def _take_attachments(self, message: str) -> str:
        if not self._pending_attachments:
            return message
        paths = self._pending_attachments
        self._pending_attachments = []
        listed = "\n".join(f"- {path}" for path in paths)
        return (
            f"{message}\n\nAttached workspace files for this message:\n{listed}\n"
            "Inspect any relevant attachment with your available file or image tools before answering."
        )

    def request_steer(self, instruction: str) -> bool:
        """Record an immediate work-direction update and interrupt active CLI turns."""
        instruction = instruction.strip()
        if not instruction:
            self.printer("System", "usage: /steer <additional instruction>")
            return False
        self._record("You", f"[Additional instruction for the work in progress]\n{instruction}")
        self._last_sent_index = {name: 0 for name in self.agents}
        steerable = False
        for agent in self.agents.values():
            cancel = getattr(agent, "cancel_current", None)
            if callable(cancel):
                steerable = True
                cancel()
        self.printer("System", "steering accepted: current work will restart with the additional instruction")
        return steerable

    def _handle_model_command(self, rest: str) -> None:
        """Show or change adapter models without risking delta-only context loss."""
        target_name, separator, requested = rest.strip().partition(" ")
        if not target_name:
            self.printer("System", self._model_status())
            return
        target = _MENTION_TARGETS.get(target_name.lower())
        if target not in ("Claude", "Codex", "Both"):
            self.printer("System", "usage: /model [claude|blanc|codex|ade|both] [model-id|default]")
            return
        if not separator or not requested.strip():
            if target == "Both":
                self.printer("System", self._model_status())
            else:
                self.printer("System", self._model_status(target))
            return
        model = None if requested.strip().lower() in ("default", "none") else requested.strip()
        targets = tuple(self.agents) if target == "Both" else (target,)
        changed: list[str] = []
        unsupported: list[str] = []
        for agent_name in targets:
            set_model = getattr(self.agents[agent_name], "set_model", None)
            if not callable(set_model):
                unsupported.append(agent_name)
                continue
            if set_model(model):
                self._last_sent_index[agent_name] = 0
                changed.append(agent_name)
        if changed:
            label = model or "default"
            for agent_name in changed:
                self._refresh_model_label(agent_name, self.agents[agent_name])
            self.printer("System", f"model set to {label} for {', '.join(changed)}; remote session reset and full context will be restored next turn")
        if unsupported:
            self.printer("System", f"model switching unavailable for {', '.join(unsupported)}")
        if not changed and not unsupported:
            self.printer("System", "model unchanged")

    def _model_status(self, only: str | None = None) -> str:
        targets = (only,) if only else tuple(self.agents)
        values = []
        for agent_name in targets:
            agent = self.agents[agent_name]
            if getattr(agent, "model", None):
                values.append(f"{agent_name} {agent.model}")
                continue
            resolved = getattr(agent, "resolved_model", None)
            if not resolved:
                values.append(f"{agent_name} default (unresolved)")
            elif getattr(agent, "resolved_model_source", None) == "configured":
                # Read from local config, not confirmed by the CLI actually
                # running it -- say so plainly here, where a caller is asking
                # specifically to check the model, not just glancing at a title.
                values.append(f"{agent_name} {resolved} (configured default, unconfirmed)")
            else:
                values.append(f"{agent_name} {resolved} (default)")
        return "models — " + "; ".join(values)

    def _prepare_request(self, target: str) -> tuple[ChatAgent, str]:
        agent = self.agents[target]
        # A prior turn may have just discovered which concrete model this
        # agent actually runs on (Claude reports it live; Codex's default is
        # read from its own config at startup) -- reflect that before the
        # next reply's title is drawn, not only right after /model.
        self._refresh_model_label(target, agent)
        other = next(name for name in self.agents if name != target)
        cursor = self._last_sent_index[target]
        delta = self.history[cursor:]
        # A target that has never been contacted needs the compacted beginning too.
        summary = self.history_summary if cursor == 0 else ""
        return agent, build_prompt(target, other, delta, summary=summary)

    def _refresh_model_label(self, target: str, agent: ChatAgent) -> None:
        set_agent_model = getattr(self.printer, "set_agent_model", None)
        if not callable(set_agent_model):
            return
        set_agent_model(target, format_resolved_model(agent))

    @staticmethod
    def _send(agent: ChatAgent, target: str, prompt: str, *, context: str | None = None) -> str:
        try:
            if context is None:
                reply = agent.send(f"chat-{target.lower()}", prompt)
            else:
                reply = agent.send(f"chat-{target.lower()}", prompt, context=context)
        except Exception as exc:  # noqa: BLE001 -- keep an adapter failure inside the REPL
            # This internal signal, unlike reply text, cannot be forged by a
            # model response. User steering is intentional interruption, not
            # evidence that the provider is unavailable.
            if not isinstance(exc, ChatAdapterInterrupted):
                try:
                    setattr(agent, "last_send_failed", True)
                except (AttributeError, TypeError):
                    pass
            return f"(error: {exc})"
        try:
            setattr(agent, "last_send_failed", False)
        except (AttributeError, TypeError):
            pass
        return reply

    def _request(self, agent: ChatAgent, target: str, prompt: str, *, context: str | None = None) -> str:
        """Send once, then silently rehydrate a newly-reset remote session."""
        reply = self._send(agent, target, prompt, context=context)
        if not getattr(agent, "last_reset", False):
            return reply

        # The adapter had to create a new remote session after a resume failure.
        # Its first retry only knew the incremental prompt, so discard that reply
        # and immediately establish the new session with all retained context.
        self._last_sent_index[target] = 0
        _, full_prompt = self._prepare_request(target)
        return self._send(agent, target, full_prompt, context=context)

    def _print_timing(self, agent: ChatAgent) -> None:
        if not self.show_timing:
            return
        timing = getattr(agent, "last_timing", None)
        if timing is not None and timing.total is not None:
            self.printer("System", timing.summary())

    def _print_usage(self, target: str, agent: ChatAgent) -> None:
        usage = getattr(agent, "last_usage", None)
        if not isinstance(usage, TokenUsage):
            return
        self.usage_totals[target] = self.usage_totals[target] + usage
        self._usage_available.add(target)
        if self.show_usage:
            self.printer("System", f"{target} tokens — {usage.summary()}")

    def _complete_request(self, target: str, reply: str) -> None:
        if self.stream_printer is not None:
            self.stream_printer.flush()
        else:
            self.printer(target, reply)
        self._record(target, reply)
        record_reply = getattr(self.task_tracker, "reply", None)
        if callable(record_reply):
            record_reply(self._active_task_id, target, reply)
        # The agent has already seen all turns that preceded this reply. Its own
        # reply need not be injected on the next resumed call.
        self._last_sent_index[target] = len(self.history)
        if target == "Claude":
            self._sync_codex_network_fallback()
        self._compact_history()

    def _sync_codex_network_fallback(self) -> None:
        """Open Codex networking only while the live Claude provider is unavailable."""
        claude = self.agents.get("Claude")
        codex = self.agents.get("Codex")
        setter = getattr(codex, "set_network_access", None)
        if not callable(setter):
            return
        unavailable = bool(getattr(claude, "last_send_failed", False))
        if not setter(unavailable):
            return
        # set_network_access() resets the remote Codex session. Its next fresh
        # turn must receive all retained local context, not only the delta.
        self._last_sent_index["Codex"] = 0
        if unavailable:
            self.printer(
                "System",
                "Blanc unavailable: Aide workspace network enabled for approved GitHub fallback tasks",
            )
        else:
            self.printer(
                "System",
                "Blanc recovered: Aide fallback network disabled and remote session reset",
            )

    def _begin_task(self, message: str) -> None:
        begin = getattr(self.task_tracker, "begin", None)
        self._active_task_id = begin(message) if callable(begin) else None

    def _complete_task(self) -> None:
        complete = getattr(self.task_tracker, "complete", None)
        if callable(complete):
            complete(self._active_task_id)
        self._active_task_id = None

    def send_to(self, target: str) -> None:
        agent, prompt = self._prepare_request(target)
        self.printer("System", f"{target}: 요청 전송")
        reply = self._request(agent, target, prompt)
        self._complete_request(target, reply)
        self.printer("System", f"{target}: 응답 완료")
        self._print_timing(agent)
        self._print_usage(target, agent)
        if self.auto_relay_enabled:
            self._maybe_relay(from_agent=target, reply=reply, hops=0)

    def _maybe_relay(self, *, from_agent: str, reply: str, hops: int) -> None:
        """Continue only explicit, bounded handoffs between the two agents."""
        target = _addressed_agent(reply)
        if target is None or target == from_agent:
            return
        if hops >= _MAX_AUTO_RELAY_HOPS:
            self.printer("System", "automatic relay stopped: hop limit reached; mention an agent again to continue")
            return
        record_handoff = getattr(self.task_tracker, "handoff", None)
        if callable(record_handoff):
            record_handoff(self._active_task_id, from_agent, target, hops + 1)
        agent, prompt = self._prepare_request(target)
        context = f"relay {hops + 1}/{_MAX_AUTO_RELAY_HOPS}"
        self.printer("System", f"auto-relay {hops + 1}/{_MAX_AUTO_RELAY_HOPS}: {from_agent} -> {target}")
        next_reply = self._request(agent, target, prompt, context=context)
        self._complete_request(target, next_reply)
        self.printer("System", f"{target}: 응답 완료")
        self._print_timing(agent)
        self._print_usage(target, agent)
        self._maybe_relay(from_agent=target, reply=next_reply, hops=hops + 1)

    def handle_line(self, raw: str) -> bool:
        if not raw.strip():
            return True
        target, message = parse_input(raw, self.default_agent)
        if target is None:
            return self.handle_command(message)
        message = self._take_attachments(message)
        self._record("You", message)
        self._compact_history()
        self._begin_task(message)
        return self._dispatch(target)

    def _dispatch(self, target: str) -> bool:
        if target == "Both":
            self.printer("System", "@both: Claude → Codex 순서로 진행 중")
            # Prepare each request immediately before dispatch.  This lets the
            # second agent receive the first agent's completed answer and keeps
            # live output from interleaving across agents.
            for name in self.agents:
                agent, prompt = self._prepare_request(name)
                reply = self._request(agent, name, prompt)
                self._complete_request(name, reply)
                self.printer("System", f"{name}: 응답 완료")
                self._print_timing(agent)
                self._print_usage(name, agent)
        else:
            self.send_to(target)
        self._complete_task()
        return True

    def run(self, *, input_stream=None) -> None:
        self.printer("System", f"chat session started. default agent: {self.default_agent}. type /help for commands.")
        if input_stream is None and getattr(sys.stdin, "isatty", lambda: False)():
            self._run_interactive()
        else:
            self._run_stream(sys.stdin if input_stream is None else input_stream)
        self.printer("System", "session ended.")

    def _run_stream(self, stream) -> None:
        """Synchronous path for redirected input and deterministic tests."""
        while True:
            try:
                raw = next(stream) if hasattr(stream, "__next__") else stream.readline()
            except (StopIteration, EOFError, KeyboardInterrupt):
                break
            if raw == "":
                break
            try:
                if not self.handle_line(raw.rstrip("\n")):
                    break
            except KeyboardInterrupt:
                break

    def _run_interactive(self) -> None:
        """Keep stdin responsive while one ordered chat request is in flight.

        Agent sessions are stateful, so requests deliberately execute one at a
        time.  Reading input independently still lets the user add follow-up
        messages during a slow CLI turn; they are dispatched in arrival order
        after that turn completes.
        """
        incoming: queue.Queue[str | None] = queue.Queue()

        def read_input() -> None:
            while True:
                try:
                    sys.stdout.write("You> ")
                    sys.stdout.flush()
                    raw = sys.stdin.readline()
                except (EOFError, OSError):
                    incoming.put(None)
                    return
                if raw == "":
                    incoming.put(None)
                    return
                incoming.put(raw.rstrip("\n"))

        threading.Thread(target=read_input, name="chat-input", daemon=True).start()
        pending: queue.Queue[str] = queue.Queue()
        accepting_input = True
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-turn") as executor:
            active = None
            active_target: str | None = None
            active_steered = False
            while accepting_input or active is not None or not pending.empty():
                if active is not None and active.done():
                    try:
                        keep_running = active.result()
                    except ChatAdapterInterrupted:
                        if active_steered and active_target is not None:
                            self.printer("System", "restarting work with the latest additional instruction")
                            active = executor.submit(self._dispatch, active_target)
                            active_steered = False
                            continue
                        keep_running = True
                    except KeyboardInterrupt:
                        keep_running = True
                    active = None
                    if not keep_running:
                        accepting_input = False
                        while not pending.empty():
                            pending.get_nowait()

                try:
                    raw = incoming.get(timeout=0.05)
                except queue.Empty:
                    raw = ...
                if raw is None:
                    accepting_input = False
                elif raw is not ...:
                    if active is not None and raw.strip().lower().startswith("/steer"):
                        _, _, instruction = raw.strip().partition(" ")
                        if self.request_steer(instruction):
                            active_steered = True
                    else:
                        pending.put(raw)
                    if active is not None and not raw.strip().lower().startswith("/steer"):
                        self.printer("System", "message queued; it will be sent after the current reply")

                if active is None and not pending.empty():
                    raw = pending.get()
                    target, message = parse_input(raw, self.default_agent)
                    if target is None:
                        active = executor.submit(self.handle_command, message)
                        active_target = None
                    else:
                        # Record exactly once; a subsequent steering restart uses
                        # _dispatch so this original message is never duplicated.
                        message = self._take_attachments(message)
                        self._record("You", message)
                        self._compact_history()
                        self._begin_task(message)
                        active_target = target
                        active = executor.submit(self._dispatch, target)
