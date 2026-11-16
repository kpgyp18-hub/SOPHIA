"""Real children exercise the opted-in absolute deadline across every pipe."""
from __future__ import annotations

import subprocess
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from scripts.supervisor.runner import Runner
from scripts.supervisor.writer_lock import WriterLock
from scripts.supervisor.chat_adapter import ClaudeChatAdapter, ChatAdapterError
from scripts.supervisor.claude_adapter import ClaudeCLIAdapter
from sophia.runtime.execution_contract import ExecutionContract


class CapturedRunner(Runner):
    def start(self, *args, **kwargs):
        self.child = super().start(*args, **kwargs)
        return self.child


class RunnerDeadlineTests(unittest.TestCase):
    def assert_reaped(self, runner, task):
        self.assertIsNotNone(runner.child.poll())
        self.assertTrue(runner.writer_lock.can_start_next(task))
        self.assertFalse(any(t.is_alive() and t.name.startswith(f"sophia-{task}-")
                             for t in threading.enumerate()))

    def test_expired_request_never_starts_a_child_in_either_mode(self):
        for streaming in (False, True):
            runner = Runner(WriterLock())
            method = runner.run_capture_streaming if streaming else runner.run_capture
            with patch.object(runner, "start") as start:
                with self.assertRaises(subprocess.TimeoutExpired):
                    method("expired", [sys.executable, "-c", "print('no')"],
                           input_text="", deadline_at=time.monotonic() - 1)
            start.assert_not_called()
            self.assertNotIn("cleanup_end", runner.last_events)

    def test_unread_large_stdin_cannot_hold_the_controller_past_deadline(self):
        for streaming in (False, True):
            runner = CapturedRunner(WriterLock())
            method = runner.run_capture_streaming if streaming else runner.run_capture
            task = f"blocked-input-{streaming}"
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                method(task, [sys.executable, "-c",
                              "import time; print('ready', flush=True); time.sleep(8)"],
                       input_text="x" * 2_000_000, timeout=30, deadline_at=start + 1)
            self.assertLess(time.monotonic() - start, 6)
            self.assertIn("ready", raised.exception.output)
            self.assertIn("cleanup_end", runner.last_events)
            self.assert_reaped(runner, task)

    def test_stdout_eof_does_not_replace_original_deadline_with_five_seconds(self):
        for streaming in (False, True):
            runner = CapturedRunner(WriterLock())
            method = runner.run_capture_streaming if streaming else runner.run_capture
            task = f"closed-output-{streaming}"
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                method(task, [sys.executable, "-c",
                              "import os,time; os.close(1); os.close(2); time.sleep(8)"],
                       input_text="", deadline_at=start + 1)
            self.assertLess(time.monotonic() - start, 4)
            self.assertIn("stdout_eof", runner.last_events)
            self.assertIn("cleanup_end", runner.last_events)
            self.assert_reaped(runner, task)

    def test_input_and_both_outputs_are_consumed_concurrently(self):
        for streaming in (False, True):
            runner = CapturedRunner(WriterLock())
            method = runner.run_capture_streaming if streaming else runner.run_capture
            task = f"all-pipes-{streaming}"
            seen = []
            kwargs = {"on_line": seen.append} if streaming else {}
            code, out, err = method(
                task, [sys.executable, "-c",
                       "import sys; sys.stderr.write('e'*2_000_000); sys.stderr.flush(); "
                       "print(len(sys.stdin.read()), flush=True)"],
                input_text="x" * 2_000_000, deadline_at=time.monotonic() + 10, **kwargs)
            self.assertEqual((code, out.strip(), len(err)), (0, "2000000", 2_000_000))
            if streaming:
                self.assertEqual(seen, ["2000000"])
            self.assert_reaped(runner, task)

    def test_late_process_start_is_reaped_without_sending_input(self):
        class SlowStartRunner(CapturedRunner):
            def start(self, *args, **kwargs):
                child = super().start(*args, **kwargs)
                time.sleep(0.2)
                return child

        runner = SlowStartRunner(WriterLock())
        with self.assertRaises(subprocess.TimeoutExpired):
            runner.run_capture("startup", [sys.executable, "-c", "import time; time.sleep(8)"],
                               input_text="never sent", deadline_at=time.monotonic() + 0.1)
        self.assertNotIn("stdin_closed", runner.last_events)
        self.assert_reaped(runner, "startup")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    def test_timeout_reaps_a_grandchild_which_inherits_the_pipes(self):
        runner = CapturedRunner(WriterLock())
        descendant = None
        try:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                runner.run_capture_streaming(
                    "tree", [sys.executable, "-c",
                             "import subprocess,sys,time; "
                             "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)']); "
                             "print(p.pid, flush=True); time.sleep(8)"],
                    input_text="", deadline_at=time.monotonic() + 1)
            descendant = int(raised.exception.output.strip())
            self.assert_reaped(runner, "tree")
            listing = subprocess.run(["tasklist", "/FI", f"PID eq {descendant}", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=3)
            self.assertNotIn(f'"{descendant}"', listing.stdout)
        finally:
            if descendant is not None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(descendant)],
                               capture_output=True, check=False, timeout=3)

    def test_callback_exception_reaps_the_child_before_propagating(self):
        runner = CapturedRunner(WriterLock())
        def fail(_line):
            raise ValueError("callback failed")
        with self.assertRaisesRegex(ValueError, "callback failed"):
            runner.run_capture_streaming("callback", [sys.executable, "-c",
                                          "import time; print('ready', flush=True); time.sleep(8)"],
                                         input_text="", deadline_at=time.monotonic() + 5,
                                         on_line=fail)
        self.assert_reaped(runner, "callback")

    def test_unverified_cleanup_blocks_next_execution(self):
        runner = CapturedRunner(WriterLock())
        try:
            with patch.object(runner, "_cleanup_deadline_process", side_effect=TimeoutError):
                with self.assertRaisesRegex(RuntimeError, "cleanup unverified"):
                    runner.run_capture("uncertain", [sys.executable, "-c",
                                                     "import time; time.sleep(8)"],
                                       input_text="", deadline_at=time.monotonic() + 0.2)
            self.assertNotIn("cleanup_end", runner.last_events)
            with self.assertRaisesRegex(RuntimeError, "reconcile"):
                runner.start("uncertain", [sys.executable, "-c", "print('no')"])
            with self.assertRaisesRegex(RuntimeError, "reconcile"):
                runner.start("other-task", [sys.executable, "-c", "print('no')"])
        finally:
            # The test's injected cleanup failure must not leave its real child behind.
            runner._cleanup_deadline_process(runner.child, [])

    def test_chat_adapter_passes_the_absolute_deadline_through_to_runner(self):
        class LocalChat(ClaudeChatAdapter):
            def _command(self):
                return [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(8)"]

        for streaming in (False, True):
            runner = CapturedRunner(WriterLock())
            adapter = LocalChat(runner, cwd=".", executable_path=sys.executable,
                                show_progress=False, timeout_seconds=30,
                                on_line=(lambda _: None) if streaming else None)
            budget = ExecutionContract.start("live_chat", "chat-claude", "initial", 1)
            start = time.monotonic()
            with self.assertRaises(ChatAdapterError):
                adapter.send("chat-claude", "x" * 2_000_000, execution_contract=budget)
            self.assertLess(time.monotonic() - start, 6)
            self.assertEqual(adapter.last_timing.outcome, "timeout")
            self.assert_reaped(runner, "chat-claude")

    def test_command_construction_does_not_refresh_cli_deadline(self):
        class SlowCommand(ClaudeCLIAdapter):
            def _command(self, task):
                time.sleep(0.15)
                return [sys.executable, "-c", "print('never started')"]

        runner = Runner(WriterLock())
        adapter = SlowCommand(runner, executable_path=sys.executable, show_progress=False)
        budget = ExecutionContract.start("agent_supervisor", "task", "attempt", 0.1)
        with patch.object(runner, "start") as start:
            with self.assertRaises(subprocess.TimeoutExpired):
                adapter.execute({"step_id": "01-plan"}, execution_contract=budget)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
