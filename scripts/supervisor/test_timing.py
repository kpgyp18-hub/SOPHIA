"""Tests for turn-level latency measurement (scripts/supervisor/timing.py)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.supervisor.timing import TimingLog, TurnTiming


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


if __name__ == "__main__":
    unittest.main()
