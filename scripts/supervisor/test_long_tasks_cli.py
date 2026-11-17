"""The detached test surface cannot widen into arbitrary or whole-suite jobs."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import long_tasks
from scripts.supervisor.long_task import LongTaskError, LongTaskHost, _write_state


class LongTasksCLITests(unittest.TestCase):
    def test_only_previously_verified_exact_test_files_are_admitted(self):
        command = long_tasks.test_command(["scripts/supervisor/test_usage.py"])
        self.assertEqual(command[1:3], ["-X", "utf8"])
        self.assertEqual(command[3], str(long_tasks.REPO_ROOT / "scripts/run_tests.py"))
        for target in ("tests", "scripts/supervisor", "--allow-full", "--override-ini=addopts=",
                       "tests/test_cognitive_os_milestones.py", "../outside.py", "scripts/chat.py"):
            with self.subTest(target=target), self.assertRaises(LongTaskError):
                long_tasks.test_command([target])

    def test_disabled_start_creates_no_job(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOPHIA_LONG_TASKS": "0"}):
            root = Path(tmp) / "jobs"
            with redirect_stdout(io.StringIO()):
                code = long_tasks.main(["--root", str(root), "start-tests", "one", "--deadline-seconds", "30",
                                        "scripts/supervisor/test_usage.py"])
            self.assertEqual(code, 1)
            self.assertFalse(root.exists())

    def test_start_passes_only_the_fixed_wrapper_command_and_explicit_deadline(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(LongTaskHost, "start", return_value={"status": "RUNNING"}) as start:
            with redirect_stdout(io.StringIO()):
                code = long_tasks.main(["--root", tmp, "start-tests", "one", "--deadline-seconds", "42",
                                        "scripts/supervisor/test_usage.py"])
            self.assertEqual(code, 0)
            self.assertEqual(start.call_args.kwargs["deadline_s"], 42)
            self.assertEqual(start.call_args.kwargs["cwd"], str(long_tasks.REPO_ROOT))
            self.assertEqual(start.call_args.args[1], long_tasks.test_command(["scripts/supervisor/test_usage.py"]))

    def test_read_commands_work_when_execution_flag_is_off(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOPHIA_LONG_TASKS": "0"}):
            task = Path(tmp) / "old"
            task.mkdir()
            _write_state(task, {"task_id": "old", "status": "FINISHED", "outcome": "completed", "cleanup": "child_gone"})
            for command in ("list", "status", "collect"):
                output = io.StringIO()
                args = ["--root", tmp, command] + ([] if command == "list" else ["old"])
                with redirect_stdout(output):
                    self.assertEqual(long_tasks.main(args), 0)
                self.assertTrue(json.loads(output.getvalue()))

    def test_unknown_or_unreadable_job_is_visible_and_cannot_be_implicitly_released(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOPHIA_LONG_TASKS": "1"}):
            task = Path(tmp) / "old"
            task.mkdir()
            (task / "state.json").write_text("{broken")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(long_tasks.main(["--root", tmp, "list"]), 0)
            self.assertEqual(json.loads(output.getvalue())[0]["status"], "UNKNOWN")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(long_tasks.main(["--root", tmp, "reconcile", "old", "--note", "verified no work"]), 0)
            self.assertEqual(LongTaskHost(tmp).status("old")["outcome"], "reconciled")
