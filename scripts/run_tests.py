"""Run pytest with per-run isolated basetemp/cache and preserved evidence (patch 01a).

``pytest.ini`` pins ``cache_dir`` and ``--basetemp`` to fixed paths, so two
concurrent sessions fight over the same directories (WinError 5).  This wrapper
leaves ``pytest.ini`` untouched and overrides both per run, under
``workspace/test_runs/<run_id>/``, next to the JUnit XML, logs and a ``run.json``
record (run id, wrapper PID, argv, start/end, exit code).

A run with no explicit test target collects the whole suite, which the vault
rules reserve for approved bounded runs; it is refused unless ``--allow-full``
is passed.

Usage:  python scripts/run_tests.py [--allow-full] [--runs-dir DIR] <pytest args...>
Rollback: delete this file and tests/test_run_tests_wrapper.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "workspace" / "test_runs"
_VALUE_OPTIONS = {"-k", "-m", "-p", "-o", "-c", "--deselect", "--ignore", "--ignore-glob",
                  "--junitxml", "--basetemp", "--rootdir", "--maxfail", "-W", "--durations"}


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def has_explicit_target(args: list[str]) -> bool:
    """True if any positional argument names a file, directory or node id."""
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in _VALUE_OPTIONS:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return True
    return False


def build_command(args: list[str], run_dir: Path) -> list[str]:
    # Later --basetemp / -o wins over addopts and pytest.ini, so these override the fixed paths.
    return [sys.executable, "-m", "pytest", *args,
            f"--basetemp={run_dir / 'basetemp'}",
            "-o", f"cache_dir={run_dir / 'cache'}",
            f"--junitxml={run_dir / 'junit.xml'}"]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    allow_full = "--allow-full" in args
    runs_dir = DEFAULT_RUNS_DIR
    if "--runs-dir" in args:
        i = args.index("--runs-dir")
        runs_dir = Path(args[i + 1])
        del args[i:i + 2]
    args = [a for a in args if a != "--allow-full"]
    if not allow_full and not has_explicit_target(args):
        print("refused: no explicit test target (would collect the whole suite); "
              "pass files/node ids, or --allow-full for an approved bounded run", file=sys.stderr)
        return 2
    run_id = new_run_id()
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    cmd = build_command(args, run_dir)
    record = {"run_id": run_id, "wrapper_pid": os.getpid(), "argv": cmd, "cwd": str(ROOT),
              "started_at": datetime.now().isoformat(timespec="seconds"), "exit_code": None}
    (run_dir / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    t0 = time.monotonic()
    with open(run_dir / "stdout.log", "wb") as out, open(run_dir / "stderr.log", "wb") as err:
        code = subprocess.call(cmd, cwd=ROOT, stdout=out, stderr=err)
    record.update(exit_code=code, ended_at=datetime.now().isoformat(timespec="seconds"),
                  duration_s=round(time.monotonic() - t0, 2))
    (run_dir / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"run_id={run_id} exit={code} dir={run_dir}")
    return code


if __name__ == "__main__":
    sys.exit(main())
