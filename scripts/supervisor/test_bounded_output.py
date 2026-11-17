"""Finite capture and queue limits with real local children, no provider calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from .runner import Runner, OutputLimits, OutputLimitExceeded
from .writer_lock import WriterLock
from .chat_adapter import ClaudeChatAdapter
from .timing import TimingLog
from sophia.runtime.execution_contract import ExecutionContract


class CapturedRunner(Runner):
    def start(self, *args, **kwargs):
        self.child = super().start(*args, **kwargs)
        return self.child


class BoundedOutputTests(unittest.TestCase):
    def runner(self, **limits):
        return CapturedRunner(WriterLock(), output_limits=OutputLimits(**limits))

    def run_child(self, runner, code, *, callback=None, timeout=5):
        return runner.run_capture_streaming("bounded", [sys.executable, "-X", "utf8", "-c", code], input_text="",
                                            timeout=timeout, on_line=callback)

    def assert_reaped(self, runner):
        self.assertIsNotNone(runner.child.poll())
        self.assertFalse(any(t.is_alive() and t.name.startswith("sophia-bounded-") for t in threading.enumerate()))
        self.assertTrue(runner.writer_lock.can_start_next("bounded"))

    def test_stdout_or_stderr_volume_is_never_truncated_into_success(self):
        for channel in ("stdout", "stderr"):
            with self.subTest(channel=channel):
                runner = self.runner(max_bytes=4096)
                with self.assertRaises(OutputLimitExceeded) as raised:
                    self.run_child(runner, f"import sys;sys.{channel}.write('x'*200000);sys.{channel}.flush()")
                self.assertLessEqual(len((raised.exception.output + raised.exception.stderr).encode()), 4096)
                self.assertEqual(runner.last_event_details["output_limit"]["kind"], "bytes")
                self.assertIn("cleanup_end", runner.last_events)
                self.assert_reaped(runner)

    def test_single_line_and_chunk_count_have_independent_caps(self):
        for limits, code, expected in (
            ({"max_bytes": 262144, "max_line_bytes": 1024}, "print('x'*100000)", "line_bytes"),
            ({"max_chunks": 4}, "for i in range(20): print('small')", "chunks"),
        ):
            with self.subTest(expected=expected):
                runner = self.runner(**limits)
                with self.assertRaises(OutputLimitExceeded):
                    self.run_child(runner, code)
                self.assertEqual(runner.last_event_details["output_limit"]["kind"], expected)
                self.assert_reaped(runner)

    def test_slow_callback_applies_bounded_backpressure_without_losing_lines(self):
        runner = self.runner(queue_lines=2, max_chunks=1000)
        seen = []
        def callback(line):
            seen.append(line)
            time.sleep(.002)
        code, out, _ = self.run_child(runner, "for i in range(200): print(i)", callback=callback)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [str(i) for i in range(200)])
        self.assertEqual(len(out.splitlines()), 200)
        self.assertLessEqual(runner.last_output_stats["queue_peak"], 2)
        self.assert_reaped(runner)

    def test_deadline_cleans_threads_even_when_queue_is_full(self):
        runner = self.runner(queue_lines=1)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_child(runner, "import time\nfor i in range(500): print(i,flush=True)\ntime.sleep(5)",
                           callback=lambda line: time.sleep(.02), timeout=.15)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(runner.last_event_details["timeout"]["stage"], "hard_deadline")
        self.assert_reaped(runner)

    def test_valid_unicode_and_non_newline_tail_are_preserved(self):
        runner = self.runner()
        seen = []
        code, out, err = self.run_child(runner, "import sys;sys.stdout.write('안녕\\n끝');sys.stderr.write('경고\\n')", callback=seen.append)
        self.assertEqual((code, out, err), (0, "안녕\n끝", "경고\n"))
        self.assertEqual(seen, ["안녕", "끝"])

    def test_limit_details_and_retained_size_reach_adapter_timing_log(self):
        class LocalAdapter(ClaudeChatAdapter):
            def _command(self):
                return [sys.executable, "-c", "print('x'*200000)"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            adapter = LocalAdapter(self.runner(max_bytes=4096), cwd=tmp, executable_path=sys.executable,
                                   show_progress=False, timing_log=TimingLog(path))
            with self.assertRaises(OutputLimitExceeded):
                adapter.send("bounded", "hi")
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["outcome"], "error")
            self.assertEqual(row["event_details"]["output_limit"]["kind"], "bytes")
            self.assertLessEqual(row["output_stats"]["stdout_bytes"], 4096)

    def test_flag_works_in_capture_mode_and_unbounded_lifetime_is_rejected(self):
        with patch.dict(os.environ, {"SOPHIA_G2_BOUNDED_OUTPUT": "1"}):
            runner = self.runner()
            with patch.object(runner, "start") as start:
                with self.assertRaises(ValueError):
                    runner.run_capture("bounded", ["never"], input_text="")
                start.assert_not_called()
            runner = CapturedRunner(WriterLock())
            result = runner.run_capture("bounded", [sys.executable, "-c", "print('ok')"], input_text="", timeout=3)
            self.assertEqual((result[0], result[1].strip()), (0, "ok"))
            self.assertEqual(runner.last_output_stats["queue_capacity"], 128)

    def test_attempt_expiring_before_runner_does_not_reuse_old_event_details(self):
        runner = self.runner()
        runner.last_events = {"process_start": 0.0, "timeout": 0.1}
        runner.last_event_details = {"timeout": {"stage": "old"}}
        runner.last_output_stats = {"stdout_bytes": 123}
        adapter = ClaudeChatAdapter(runner, cwd=".", executable_path="never", show_progress=False)
        budget = ExecutionContract("live_chat", "bounded", "expired", time.monotonic()-2, time.monotonic()-1)
        with self.assertRaises(TimeoutError):
            adapter._run("bounded", ["never"], "hi", execution_contract=budget)
        self.assertEqual(adapter.last_timing.events, {})
        self.assertEqual(adapter.last_timing.event_details, {})
        self.assertEqual(adapter.last_timing.output_stats, {})
