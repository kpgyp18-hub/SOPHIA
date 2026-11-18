"""Read-only timing summary. Phase labels describe stdout, not remote causes."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHASES = {"before_start", "input", "no_output", "tool_running", "silent_after_progress",
          "active_until_deadline", "terminal_seen_exit_hung"}
OUTCOMES = {"completed", "nonzero_exit", "timeout", "cancelled", "error"}
NOTE = ("Phases describe observed stdout state, not a timeout cause. Codex JSON silence can include "
        "message generation. Non-streaming captures have no per-line times; legacy rows remain separate.")


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def summarize(path: Path) -> dict:
    groups = {}
    rows = malformed = legacy = 0
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                row = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue
            rows += 1
            progress = row.get("progress")
            if not isinstance(progress, dict) or not progress:
                progress = {}
                legacy += 1
            label = row.get("label")
            label = label if label in ("Claude", "Codex") else "unknown"
            outcome = row.get("outcome")
            outcome = outcome if isinstance(outcome, str) and outcome in OUTCOMES else "unknown"
            phase = progress.get("timeout_phase")
            phase = (phase if isinstance(phase, str) and phase in PHASES
                     else ("legacy" if not progress else "unavailable"))
            key = (label, outcome, phase)
            group = groups.setdefault(key, {"label": label, "outcome": outcome, "timeout_phase": phase,
                "count": 0, "duration_count": 0, "total_s": 0.0, "max_tail_idle_s": None,
                "stdout_lines_count": 0, "stdout_lines_sum": 0, "stdout_lines_max": None,
                "stdout_line_buckets": {"0-100": 0, "101-1000": 0, "1001-10000": 0,
                                        "10001-20000": 0, "20001+": 0}})
            group["count"] += 1
            duration = number(row.get("total_s"))
            if duration is not None:
                group["duration_count"] += 1
                group["total_s"] += duration
            idle = number(progress.get("tail_idle_s"))
            if idle is not None:
                group["max_tail_idle_s"] = max(idle, group["max_tail_idle_s"] or 0)
            lines = progress.get("stdout_lines")
            if type(lines) is int and lines >= 0:
                group["stdout_lines_count"] += 1
                group["stdout_lines_sum"] += lines
                group["stdout_lines_max"] = max(lines, group["stdout_lines_max"] or 0)
                bucket = next((name for limit, name in ((100, "0-100"), (1000, "101-1000"),
                              (10000, "1001-10000"), (20000, "10001-20000")) if lines <= limit), "20001+")
                group["stdout_line_buckets"][bucket] += 1
    result = []
    for key in sorted(groups):
        group = groups[key]
        group["mean_total_s"] = (round(group["total_s"] / group["duration_count"], 6)
                                 if group["duration_count"] else None)
        group["mean_stdout_lines"] = (round(group["stdout_lines_sum"] / group["stdout_lines_count"], 3)
                                      if group["stdout_lines_count"] else None)
        result.append(group)
    return {"rows": rows, "malformed_rows": malformed, "legacy_rows": legacy, "groups": result, "note": NOTE}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=ROOT / "workspace/chat_latency.jsonl")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = summarize(args.path)
    except OSError as exc:
        print(f"cannot read timing log: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"rows={report['rows']} malformed={report['malformed_rows']} legacy={report['legacy_rows']}")
        print("label outcome timeout_phase count mean_s max_stdout_lines")
        for group in report["groups"]:
            print(" ".join(str(group[key]) for key in
                  ("label", "outcome", "timeout_phase", "count", "mean_total_s", "stdout_lines_max")))
        print(NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
