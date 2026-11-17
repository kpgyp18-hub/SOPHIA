"""Explicit, bounded test jobs with durable status and later result collection.

Usage: python scripts/long_tasks.py start-tests TASK_ID --deadline-seconds 120
           scripts/supervisor/test_usage.py
       python scripts/long_tasks.py list|status TASK_ID|collect TASK_ID|cancel TASK_ID

Start and reconciliation require SOPHIA_LONG_TASKS=1. No arbitrary commands,
pytest flags, directory targets, or whole-suite entrypoints are accepted.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.supervisor.long_task import LongTaskError, LongTaskHost, LongTasksDisabled

# These target modules have already been exercised independently of the Core's
# nested whole-suite runner. Expand only after checking the target's call path.
APPROVED_TESTS = frozenset(f"scripts/supervisor/{name}.py" for name in (
    "test_usage", "test_long_task", "test_chat_recovery", "test_cli_diagnostics",
    "test_chat_session", "test_timing", "test_completion", "test_runner_deadline",
    "test_supervisor", "test_supervisor_recovery",
))


def test_command(targets: list[str]) -> list[str]:
    approved = {(REPO_ROOT / relative).resolve(): relative for relative in APPROVED_TESTS}
    selected = []
    for target in targets:
        path = Path(target)
        path = path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()
        if path not in approved or not path.is_file():
            raise LongTaskError(f"test target is not approved for detached execution: {target}")
        if approved[path] not in selected:
            selected.append(approved[path])
    if not selected:
        raise LongTaskError("at least one approved test file is required")
    return [sys.executable, "-X", "utf8", str(REPO_ROOT / "scripts/run_tests.py"), *selected, "-q"]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Long-task evidence directory (default: workspace/long_tasks).")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start-tests", help="Run approved test files with an explicit hard deadline.")
    start.add_argument("task_id")
    start.add_argument("--deadline-seconds", type=float, required=True)
    start.add_argument("targets", nargs="+", help="Exact approved test files; no pytest flags or directories.")
    commands.add_parser("list")
    for name in ("status", "collect", "cancel", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("task_id")
        if name == "reconcile":
            command.add_argument("--note", required=True, help="What process and side-effect state was verified.")
    args = parser.parse_args(argv)
    host = LongTaskHost(args.root)
    try:
        if args.command == "start-tests":
            state = host.start(args.task_id, test_command(args.targets),
                               deadline_s=args.deadline_seconds, cwd=str(REPO_ROOT))
        elif args.command == "list":
            state = host.list_tasks()
        elif args.command == "reconcile":
            state = host.reconcile(args.task_id, note=args.note)
        else:
            state = getattr(host, args.command)(args.task_id)
    except (LongTaskError, LongTasksDisabled, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
