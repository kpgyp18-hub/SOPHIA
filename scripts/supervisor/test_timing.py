"""Tests for turn-level latency measurement (scripts/supervisor/timing.py)."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.supervisor.chat_adapter import ClaudeChatAdapter
from scripts.supervisor.timing import TimingLog, TurnTiming
from scripts.supervisor.usage import UsageLog


class TurnTimingTests(unittest.TestCase):
    def test_incomplete_turn_has_no_durations(self) -> None:
        timing = TurnTiming(label="Claude", start=100.0)
        self.assertIsNone(timing.total)
        self.assertIsNone(timing.to_first_output)
        self.assertIn("incomplete", timing.summary())

    def test_reports_lead_time_when_first_output_precedes_completion(self) -> None:
        timing = TurnTiming(label="Claude", start=100.0, first_output=102.5, end=110.0)
        self.assertAlmostEqual(timing.to_first_output, 2.5)
        self.assertAlmostEqual(timing.total, 10.0)
        summary = timing.summary()
        self.assertIn("10.0s", summary)
        self.assertIn("2.5s", summary)

    def test_omits_lead_time_when_indistinguishable_from_completion(self) -> None:
        # Non-streaming calls set first_output right at completion; the gap is
        # microseconds, not worth reporting as a separate figure.
        timing = TurnTiming(label="Codex", start=100.0, first_output=108.0, end=108.0001)
        summary = timing.summary()
        self.assertNotIn("첫 출력", summary)
        self.assertIn("8.0s", summary)


class TimingLogTests(unittest.TestCase):
    def test_none_path_is_a_silent_no_op(self) -> None:
        log = TimingLog(None)
        log.record(TurnTiming(label="Claude", start=0.0, first_output=1.0, end=2.0))  # must not raise

    def test_appends_one_json_line_per_recorded_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "latency.jsonl"
            log = TimingLog(path)
            log.record(TurnTiming(label="Claude", start=0.0, first_output=1.5, end=4.0))
            log.record(TurnTiming(label="Codex", start=0.0, first_output=2.0, end=2.0))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["label"], "Claude")
            self.assertAlmostEqual(first["total_s"], 4.0)
            self.assertAlmostEqual(first["to_first_output_s"], 1.5)


class TurnOutcomeFieldsTests(unittest.TestCase):
    """Patch 02 (P0-B): outcome / exit_code / last_output_s / started_at."""

    def test_row_carries_the_four_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            timing = TurnTiming(label="Claude", start=10.0, first_output=11.0, end=20.0,
                                last_output=12.5, outcome="timeout", exit_code=None)
            TimingLog(path).record(timing)
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["outcome"], "timeout")
            self.assertIsNone(row["exit_code"])
            self.assertAlmostEqual(row["last_output_s"], 2.5)
            self.assertRegex(row["started_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_old_fields_are_unchanged(self) -> None:
        timing = TurnTiming(label="Claude", start=0.0, first_output=1.0, end=3.0)
        self.assertIsNone(timing.last_output_s)
        self.assertIsNone(timing.outcome)
        self.assertAlmostEqual(timing.total, 3.0)

    def _adapter(self, runner) -> ClaudeChatAdapter:
        return ClaudeChatAdapter(runner, cwd="C:/work", executable_path="claude", show_progress=False)

    def test_adapter_classifies_completed_and_nonzero_exit(self) -> None:
        class Runner:
            def __init__(self, code): self.code = code
            def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None):
                return self.code, "", ""
        for code, expected in ((0, "completed"), (3, "nonzero_exit")):
            adapter = self._adapter(Runner(code))
            adapter._run("t1", ["claude"], "hi")
            self.assertEqual(adapter.last_timing.outcome, expected)
            self.assertEqual(adapter.last_timing.exit_code, code)
            self.assertIsNotNone(adapter.last_timing.last_output)

    def test_adapter_classifies_timeout_and_other_errors(self) -> None:
        class Runner:
            def __init__(self, exc): self.exc = exc
            def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None):
                raise self.exc
        for exc, expected in ((subprocess.TimeoutExpired(["claude"], 1), "timeout"), (RuntimeError("x"), "error")):
            adapter = self._adapter(Runner(exc))
            with self.assertRaises(type(exc)):
                adapter._run("t1", ["claude"], "hi")
            self.assertEqual(adapter.last_timing.outcome, expected)
            self.assertIsNone(adapter.last_timing.exit_code)

    def test_live_log_rows_join_across_new_resumed_and_compacted_attempts(self) -> None:
        class Runner:
            def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None):
                return 0, ('{"type":"result","session_id":"remote-1","result":"OK",'
                           '"usage":{"input_tokens":4,"output_tokens":2}}'), ""

        with tempfile.TemporaryDirectory() as tmp:
            latency = Path(tmp) / "latency.jsonl"
            usage = Path(tmp) / "usage.jsonl"
            adapter = ClaudeChatAdapter(Runner(), cwd="C:/work", executable_path="claude",
                                        show_progress=False, timing_log=TimingLog(latency),
                                        usage_log=UsageLog(usage))
            adapter.send("task", "first")
            adapter.send("task", "second")
            adapter.telemetry_reset_reason = "token_threshold"
            adapter.reset_session()
            adapter.send("task", "third")
            times = [json.loads(line) for line in latency.read_text(encoding="utf-8").splitlines()]
            uses = [json.loads(line) for line in usage.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["session_mode"] for row in times], ["new", "resumed", "new"])
        self.assertEqual([row["reset_reason"] for row in times], [None, None, "token_threshold"])
        self.assertEqual(len({row["turn_id"] for row in times}), 3)
        for timing, tokens in zip(times, uses):
            self.assertEqual((timing["turn_id"], timing["attempt_id"]),
                             (tokens["turn_id"], tokens["attempt_id"]))


if __name__ == "__main__":
    unittest.main()
