"""Friendly entry point for the Supervisor Core (Claude <-> Codex agent handoff).

Wraps scripts/supervisor/supervisor.py with:
- route shortcuts (--plan-only / --direct / --review) instead of raw route names
- repo-root-relative skills/runtime paths, so it works from any cwd
- an auto-generated task id (timestamped) when --id is not given

Examples:
  python scripts/agents.py --real "Implement X" --review
  python scripts/agents.py --dry-run CLAUDE_CODEX
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.supervisor.call_graph import CLAUDE_CODEX, CLAUDE_CODEX_REVIEW, CLAUDE_ONLY, DIRECT_CODEX, EXCEPTION_GRAPH
from scripts.supervisor.supervisor import Supervisor, _dry_run_adapters, _real_cli_adapters
from scripts.supervisor.stream_text import extract_display_text as _extract_display_text
from scripts.supervisor.timing import TimingLog

_ROUTES = {"DIRECT_CODEX": DIRECT_CODEX, "CLAUDE_ONLY": CLAUDE_ONLY, "CLAUDE_CODEX": CLAUDE_CODEX,
           "CLAUDE_CODEX_REVIEW": CLAUDE_CODEX_REVIEW, "EXCEPTION_GRAPH": EXCEPTION_GRAPH}


class WatchPrinter:
    """Streams only recognized message content to the terminal (see
    _extract_display_text); a still-open line (no trailing newline yet) is flushed
    via `flush()` before any other presentation output interrupts it."""

    def __init__(self) -> None:
        self._last_agent: str | None = None
        self._mid_line = False

    def on_line(self, agent: str, line: str) -> None:
        text = _extract_display_text(line)
        if not text:
            return
        if self._mid_line and self._last_agent != agent:
            print()
            self._mid_line = False
        if not self._mid_line:
            print(f"[{agent}] ", end="", flush=True)
        print(text, end="", flush=True)
        self._last_agent, self._mid_line = agent, True

    def flush(self) -> None:
        if self._mid_line:
            print()
            self._mid_line = False


def _select_route(args: argparse.Namespace) -> tuple[str, tuple[str, ...]]:
    if args.route: return args.route, _ROUTES[args.route]
    if args.plan_only: return "CLAUDE_ONLY", CLAUDE_ONLY
    if args.direct: return "DIRECT_CODEX", DIRECT_CODEX
    if args.review: return "CLAUDE_CODEX_REVIEW", CLAUDE_CODEX_REVIEW
    return "CLAUDE_CODEX", CLAUDE_CODEX


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Supervisor Core agent task (Claude <-> Codex).")
    parser.add_argument("description", nargs="?", help="Task description for the initial agent (required for --real).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--real", action="store_true", help="Invoke the real Claude/Codex CLIs.")
    mode.add_argument("--dry-run", action="store_true", help="Use canned FakeAdapter responses; no live CLI calls.")
    route_group = parser.add_mutually_exclusive_group()
    route_group.add_argument("--plan-only", action="store_true", help="Route: CLAUDE only.")
    route_group.add_argument("--direct", action="store_true", help="Route: CODEX only, no planning step.")
    route_group.add_argument("--review", action="store_true", help="Route: CLAUDE plan -> CODEX exec -> CLAUDE review.")
    route_group.add_argument("--route", choices=sorted(_ROUTES), help="Explicit route name (overrides the shortcuts above).")
    parser.add_argument("--id", dest="task_id", help="Task id (default: auto-generated timestamp).")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--cwd", help="Working directory for the spawned CLI subprocess (real mode only).")
    parser.add_argument("--skills-root", default=str(REPO_ROOT / "skills"))
    parser.add_argument("--runtime-root", default=str(REPO_ROOT / "workspace" / "runtime"))
    parser.add_argument("--watch", action="store_true",
                        help="Real mode only: stream each agent's raw CLI output live instead of only "
                             "printing the final decision once a step completes.")
    parser.add_argument("--no-progress", action="store_true", help="Don't show the 'step running' progress indicator.")
    parser.add_argument("--no-timing-log", action="store_true",
                        help="Don't append per-step latency records to workspace/route_latency.jsonl.")
    args = parser.parse_args()

    if args.real and not args.description:
        parser.error("a task description is required for --real runs")
    if args.watch and not args.real:
        parser.error("--watch only applies to --real runs")

    route_name, route = _select_route(args)
    task_id = args.task_id or f"{'real' if args.real else 'dry'}-{route_name.lower().replace('_', '-')}-{datetime.now():%Y%m%d-%H%M%S}"

    if args.real:
        watch_printer = WatchPrinter() if args.watch else None
        if watch_printer is not None:
            from scripts.supervisor import presentation
            presentation.set_before_emit_hook(watch_printer.flush)
        timing_log = None if args.no_timing_log else TimingLog(REPO_ROOT / "workspace" / "route_latency.jsonl")
        adapters = _real_cli_adapters(args.timeout_seconds, args.cwd, watch_printer.on_line if watch_printer else None,
                                      show_progress=not args.no_progress, timing_log=timing_log)
        supervisor = Supervisor(args.runtime_root, args.skills_root, adapters, initial_description=args.description)
    else:
        adapters = _dry_run_adapters(route)
        supervisor = Supervisor(args.runtime_root, args.skills_root, adapters, initial_description=args.description)

    state = supervisor.run(task_id, route)
    print(f"task_id={task_id} status={state['status']}")
    return 0 if state["status"] == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
