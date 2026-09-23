"""Interactive chat CLI: a shared group chat between you, Claude, and Codex, with
real file/tool access (not the protocol-only Machine Contract used by agents.py).

Examples:
  python scripts/chat.py
  python scripts/chat.py --cwd F:\\전자두뇌\\some_project --default codex
  python scripts/chat.py --dry-run          # no live CLI calls, canned echo replies
  python scripts/chat.py --no-stream        # print each reply once it's fully done
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    # Windows consoles/pipes often default to a non-UTF-8 codepage; without this,
    # Korean (or any non-ASCII) chat input/output gets corrupted into lone
    # surrogates, which then crashes on write instead of just displaying wrong.
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

from scripts.supervisor.chat_adapter import ClaudeChatAdapter, CodexChatAdapter, format_resolved_model
from scripts.supervisor.chat_presentation import ChatPresenter
from scripts.supervisor.chat_session import ChatAgent, ChatSession, StreamPrinter
from scripts.supervisor.runner import Runner
from scripts.supervisor.stream_text import extract_chat_display_text
from scripts.supervisor.timing import TimingLog
from scripts.supervisor.usage import UsageLog
from scripts.supervisor.writer_lock import WriterLock
from sophia.runtime.tracker import ChatTaskTracker


class FakeChatAgent:
    """--dry-run stand-in: deterministic canned reply, no subprocess, no cost."""

    def __init__(self, display_name: str) -> None:
        self.display_name = display_name
        self.model: str | None = None

    def set_model(self, model: str | None) -> bool:
        if self.model == model:
            return False
        self.model = model
        return True

    def send(self, task_id: str, prompt: str) -> str:
        last_line = prompt.strip().splitlines()[-1]
        return f"[dry-run {self.display_name} echo] {last_line}"


def _build_agents(args: argparse.Namespace, runner: Runner, on_line, timing_log: TimingLog | None,
                  usage_log: UsageLog | None) -> dict[str, ChatAgent]:
    if args.dry_run:
        return {"Claude": FakeChatAgent("Claude"), "Codex": FakeChatAgent("Codex")}
    show_progress = not args.no_progress
    claude = ClaudeChatAdapter(runner, cwd=args.cwd, timeout_seconds=args.timeout_seconds,
                                permission_mode=args.permission_mode, allowed_tools=args.allowed_tools,
                                model=args.claude_model, extra_writable_dirs=args.add_dir,
                                on_line=(lambda line: on_line("Claude", line)) if on_line else None,
                                show_progress=show_progress, timing_log=timing_log, usage_log=usage_log)
    codex = CodexChatAdapter(runner, cwd=args.cwd, timeout_seconds=args.timeout_seconds,
                              sandbox=args.sandbox, model=args.codex_model, extra_writable_dirs=args.add_dir,
                              on_line=(lambda line: on_line("Codex", line)) if on_line else None,
                              show_progress=show_progress, timing_log=timing_log, usage_log=usage_log)
    return {"Claude": claude, "Codex": codex}


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive group chat between you, Claude, and Codex.")
    parser.add_argument("--cwd", default=str(REPO_ROOT), help="Working directory both agents get real tool access to (default: repo root).")
    parser.add_argument("--add-dir", dest="add_dir", action="append", default=None,
                        help="Extra directory both agents may write to, alongside --cwd. Repeatable.")
    parser.add_argument("--default", dest="default_agent", choices=["both", "claude", "codex"], default="both",
                        help="Who plain (non-@mention) messages go to (default: both).")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--permission-mode", default="acceptEdits",
                        help="Claude --permission-mode (default: acceptEdits; auto-approves file edits).")
    parser.add_argument("--allowed-tools", dest="allowed_tools", default="Bash Read Write Edit Glob Grep",
                        help="Claude --allowedTools (default: Bash Read Write Edit Glob Grep).")
    parser.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write", "danger-full-access"],
                        help="Codex --sandbox (default: workspace-write).")
    parser.add_argument("--codex-model", default=None, help="Codex --model override.")
    parser.add_argument("--claude-model", default=None, help="Claude --model override.")
    parser.add_argument("--no-stream", action="store_true", help="Print each full reply at once instead of live-streaming tokens.")
    parser.add_argument("--no-progress", action="store_true", help="Don't show the 'waiting for reply' progress indicator.")
    parser.add_argument("--no-log", action="store_true", help="Don't write a transcript file under workspace/chat_logs/.")
    parser.add_argument("--no-timing-log", action="store_true",
                        help="Don't append per-reply latency records to workspace/chat_latency.jsonl.")
    parser.add_argument("--no-usage-log", action="store_true",
                        help="Don't show or append provider-reported token usage to workspace/chat_usage.jsonl.")
    parser.add_argument("--plain", action="store_true",
                        help="Disable the Rich TTY presentation and use plain text output.")
    parser.add_argument("--dry-run", action="store_true", help="Canned echo replies, no live CLI calls or cost.")
    args = parser.parse_args()

    log_path = None
    if not args.no_log:
        log_path = REPO_ROOT / "workspace" / "chat_logs" / f"{datetime.now():%Y%m%d-%H%M%S}.log"
    timing_log = None if (args.no_timing_log or args.dry_run) else TimingLog(REPO_ROOT / "workspace" / "chat_latency.jsonl")
    usage_log = None if (args.no_usage_log or args.dry_run) else UsageLog(REPO_ROOT / "workspace" / "chat_usage.jsonl")

    # Input TTY is the reliable signal that this is a live conversation.  The
    # chat host may relay stdout through a pipe, which must not disable the
    # presentation-only Codex pacing for an otherwise interactive session.
    interactive_input = getattr(sys.stdin, "isatty", lambda: False)()
    presenter = ChatPresenter(enabled=not args.plain)
    stream_printer = None if (args.no_stream or args.dry_run) else StreamPrinter(
        extract_chat_display_text,
        # Codex --json supplies only completed agent_message events.  Pace that
        # display in the live terminal so it feels consistent with Claude's
        # real deltas; never do this for redirected/non-interactive input.
        typewriter_speakers={"Codex"} if interactive_input else set(),
        stream_prefix=presenter.stream_prefix if presenter.enabled else None,
    )
    on_line = stream_printer.on_line if stream_printer else None

    runner = Runner(WriterLock())
    agents = _build_agents(args, runner, on_line, timing_log, usage_log)
    presenter.set_agent_models({name: format_resolved_model(agent) for name, agent in agents.items()})
    session = ChatSession(agents, default_agent=args.default_agent.capitalize(), log_path=log_path,
                          stream_printer=stream_printer, show_timing=not args.no_timing_log,
                          show_usage=not args.no_usage_log, attachment_root=Path(args.cwd),
                          printer=presenter, task_tracker=ChatTaskTracker("pc-cli"))
    session.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
