"""Patch 03: post-timeout completion reconciliation, including injected failures with real children."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from scripts.supervisor.chat_adapter import ChatAdapterError, ClaudeChatAdapter
from scripts.supervisor.completion import (COMPLETED_BEFORE_TIMEOUT, NO_OUTPUT, PARTIAL_OUTPUT,
                                            reconcile_timeout)
from scripts.supervisor.runner import Runner
from scripts.supervisor.timing import TimingLog
from scripts.supervisor.writer_lock import WriterLock

REAPED = {"cleanup_end": 1.0}
CLAUDE_RESULT = json.dumps({"type": "result", "result": "final answer"})
CODEX_DONE = (json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex answer"}})
              + chr(10) + json.dumps({"type": "turn.completed"}))


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class ReconcileTests(unittest.TestCase):
    def test_claude_result_event_is_a_completed_turn(self) -> None:
        r = reconcile_timeout(CLAUDE_RESULT + chr(10), "", REAPED)
        self.assertEqual((r.state, r.final_text, r.recoverable), (COMPLETED_BEFORE_TIMEOUT, "final answer", True))

    def test_codex_turn_completed_is_a_completed_turn(self) -> None:
        r = reconcile_timeout(CODEX_DONE, "", REAPED)
        self.assertEqual((r.state, r.final_text, r.recoverable), (COMPLETED_BEFORE_TIMEOUT, "codex answer", True))

    def test_output_without_terminal_event_is_partial_and_not_recoverable(self) -> None:
        delta = json.dumps({"type": "stream_event", "event": {"delta": {"text": "half"}}})
        r = reconcile_timeout(delta, "", REAPED)
        self.assertEqual((r.state, r.final_event_seen, r.recoverable), (PARTIAL_OUTPUT, False, False))

    def test_silence_is_no_output(self) -> None:
        r = reconcile_timeout(None, b"", REAPED)
        self.assertEqual((r.state, r.output_lines, r.recoverable), (NO_OUTPUT, 0, False))

    def test_result_is_not_recoverable_unless_the_process_is_confirmed_gone(self) -> None:
        for events in (None, {}):
            r = reconcile_timeout(CLAUDE_RESULT, "", events)
            self.assertEqual(r.state, COMPLETED_BEFORE_TIMEOUT)
            self.assertFalse(r.recoverable)

    def test_bytes_and_side_effects_are_handled_honestly(self) -> None:
        r = reconcile_timeout(CLAUDE_RESULT.encode(), b"oops", REAPED)
        self.assertEqual((r.stderr_bytes, r.side_effects), (4, "unverified"))
        self.assertIn("side effects unverified", r.describe())


class RunnerKeepsPartialOutputTests(unittest.TestCase):
    def test_both_paths_attach_partial_output_to_the_timeout(self) -> None:
        code = "import time; print('partial', flush=True); time.sleep(60)"
        for streaming in (False, True):
            runner = Runner(WriterLock())
            fn = runner.run_capture_streaming if streaming else runner.run_capture
            with self.assertRaises(subprocess.TimeoutExpired) as ctx:
                fn("t", [sys.executable, "-c", code], input_text="", timeout=1.5)
            self.assertIn("partial", str(ctx.exception.output or ""), f"streaming={streaming}")
            self.assertIn("cleanup_end", runner.last_events)


class _ShimAdapter(ClaudeChatAdapter):
    """Real adapter + real Runner, but the CLI is a tiny python script."""
    script = ""

    def _command(self):
        return [sys.executable, self.script]


class InjectedFailureTests(unittest.TestCase):
    def _adapter(self, tmp: Path, body: str, *, stream: bool) -> _ShimAdapter:
        script = tmp / "fake_cli.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        adapter = _ShimAdapter(Runner(WriterLock()), cwd=str(tmp), timeout_seconds=2.0,
                               executable_path=sys.executable, show_progress=False,
                               on_line=(lambda line: None) if stream else None,
                               timing_log=TimingLog(tmp / "latency.jsonl"))
        adapter.script = str(script)
        return adapter

    def test_result_reported_then_hang_is_recovered_and_the_timeout_stays_visible(self) -> None:
        body = (
            "import sys, time\n"
            "print(" + repr(CLAUDE_RESULT) + ", flush=True)\n"
            "time.sleep(60)\n"
        )
        for stream in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                adapter = self._adapter(Path(tmp), body, stream=stream)
                reply = adapter.send("chat-claude", "hi")
                self.assertEqual(reply, "final answer", f"stream={stream}")
                self.assertEqual(adapter.last_timing.outcome, "timeout")
                self.assertEqual(adapter.last_timing.reconciliation, COMPLETED_BEFORE_TIMEOUT)
                lines = (Path(tmp) / "latency.jsonl").read_text(encoding="utf-8").splitlines()
                row = json.loads(lines[-1])
                self.assertEqual((row["outcome"], row["reconciliation"]), ("timeout", COMPLETED_BEFORE_TIMEOUT))
                self.assertIn("cleanup_end", row["events_s"])

    def test_partial_modification_without_result_is_an_explicit_error_not_a_success(self) -> None:
        for stream in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                touched = Path(tmp) / "edited.txt"
                body = (
                    "import time\n"
                    "open(" + repr(str(touched)) + ", 'w').write('half-done')\n"
                    "print('working', flush=True)\n"
                    "time.sleep(60)\n"
                )
                adapter = self._adapter(Path(tmp), body, stream=stream)
                with self.assertRaises(ChatAdapterError) as ctx:
                    adapter.send("chat-claude", "hi")
                self.assertIn("side effects unverified", str(ctx.exception))
                self.assertIn("verify the workspace before retrying", str(ctx.exception))
                self.assertEqual(adapter.last_completion_report.state, PARTIAL_OUTPUT)
                self.assertTrue(touched.exists())  # the side effect is real and was not hidden

    def test_no_residual_process_and_no_duplicate_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "runs.txt"
            pidfile = Path(tmp) / "pid.txt"
            body = (
                "import os, time\n"
                "open(" + repr(str(counter)) + ", 'a').write('x')\n"
                "open(" + repr(str(pidfile)) + ", 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            adapter = self._adapter(Path(tmp), body, stream=True)
            with self.assertRaises(ChatAdapterError):
                adapter.send("chat-claude", "hi")
            self.assertEqual(counter.read_text(), "x")  # exactly one execution: no retry after a timeout
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertFalse(_alive(pid), "timed-out child survived cleanup")
            self.assertIs(adapter.runner.writer_lock.can_start_next("chat-claude"), True)


if __name__ == "__main__":
    unittest.main()
