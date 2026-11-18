"""Read-only summary of Core shadow decisions vs live dispatch (Core connection C1).

Rows recorded before C1 have no ``core_shadow``; they are summarised separately
from the routing fields the tracker already kept. Nothing is written.
Usage: python -B -X utf8 scripts/core_shadow_report.py [TASK_DIR] [--source pc-cli] [--json]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarize(root: Path, source: str | None = None) -> dict:
    shadow, legacy = collections.Counter(), collections.Counter()
    counts = collections.Counter()
    for path in sorted(root.glob("task-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counts["unreadable"] += 1
            continue
        if not isinstance(state, dict) or (source is not None and state.get("source") != source):
            continue
        counts["rows"] += 1
        snap = state.get("core_shadow")
        if isinstance(snap, dict) and "dispatch" in snap:
            view = snap["dispatch"]
            counts["shadow_rows"] += 1
            counts["would_wait_under_core"] += bool(view.get("would_wait_under_core"))
            counts["core_prefers_local"] += bool(view.get("core_prefers_local"))
            counts["injection_flagged"] += snap.get("injection_pattern") is not None
            counts["snapshot_errors"] += "error" in snap
            shadow[(str(view.get("core_route")), str(view.get("target")), str(view.get("approval_basis")))] += 1
        else:
            counts["legacy_rows"] += 1
            agents = sorted({e.get("agent") for e in state.get("events", []) if isinstance(e, dict) and e.get("type") == "reply"} - {None})
            legacy[(str((state.get("escalation") or {}).get("route")), "+".join(agents) or "none")] += 1
    return {"counts": dict(counts),
            "shadow": [{"core_route": k[0], "target": k[1], "approval_basis": k[2], "count": v} for k, v in shadow.most_common()],
            "legacy": [{"core_route": k[0], "replied": k[1], "count": v} for k, v in legacy.most_common()],
            "note": "Shadow basis: rules only, no provider health, no Qwen hint. Legacy rows use reply agents as the actual target."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=ROOT / "workspace/sophia/tasks")
    parser.add_argument("--source", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"task directory not found: {args.root}", file=sys.stderr)
        return 2
    report = summarize(args.root, args.source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(" ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
    for row in report["shadow"]:
        print(f"shadow route={row['core_route']} target={row['target']} basis={row['approval_basis']} count={row['count']}")
    for row in report["legacy"]:
        print(f"legacy route={row['core_route']} replied={row['replied']} count={row['count']}")
    print(report["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
