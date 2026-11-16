"""Patch 02: Runner lifecycle events and concurrent stderr draining. Uses tiny real child processes."""
from __future__ import annotations

import subprocess
import sys
import unittest

from scripts.supervisor.runner import Runner
from scripts.supervisor.writer_lock import WriterLock

PY = sys.executable


def _child(code: str) -> list[str]:
    return [PY, "-c", code]


class StreamingEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = Runner(WriterLock())

    def test_events_are_recorded_in_order_for_a_clean_run(self) -> None:
        seen: list[str] = []
        code, out, err = self.runner.run_capture_streaming(
            "t1", _child("import sys; print('hi'); sys.stderr.write('warn')"), input_text="",
            timeout=20, on_event=lambda name, t, detail: seen.append(name))
        self.assertEqual((code, out.strip(), err.strip()), (0, "hi", "warn"))
        for name in ("process_start", "first_stdout", "first_stderr", "stdout_eof", "stderr_eof", "child_exit"):
            self.assertIn(name, seen)
        self.assertEqual(seen[0], "process_start")
        self.assertLess(seen.index("first_stdout"), seen.index("child_exit"))
        self.assertEqual(set(self.runner.last_events), set(seen))

    def test_large_stderr_does_not_stall_stdout(self) -> None:
        """A child filling the stderr pipe used to block in write() until the turn deadline."""
        child = _child("import sys; sys.stderr.write('e' * 2_000_000); sys.stderr.flush(); print('done')")
        code, out, err = self.runner.run_capture_streaming("t2", child, input_text="", timeout=30)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "done")
        self.assertEqual(len(err), 2_000_000)

    def test_timeout_emits_stage_and_cleanup_then_raises(self) -> None:
        seen: list[tuple[str, dict]] = []
        with self.assertRaises(subprocess.TimeoutExpired):
            self.runner.run_capture_streaming(
                "t3", _child("import time; time.sleep(60)"), input_text="", timeout=1.0,
                on_event=lambda name, t, detail: seen.append((name, dict(detail))))
        names = [n for n, _ in seen]
        self.assertIn(("timeout", {"stage": "hard_deadline"}), seen)
        self.assertLess(names.index("timeout"), names.index("cleanup_start"))
        self.assertLess(names.index("cleanup_start"), names.index("cleanup_end"))
        self.assertIsNotNone(self.runner.writer_lock.can_start_next("t3"))

    def test_a_failing_event_sink_cannot_break_the_run(self) -> None:
        def bad_sink(name, t, detail):
            raise RuntimeError("sink bug")
        code, out, _ = self.runner.run_capture_streaming(
            "t4", _child("print('ok')"), input_text="", timeout=20, on_event=bad_sink)
        self.assertEqual((code, out.strip()), (0, "ok"))


class CaptureEventTests(unittest.TestCase):
    def test_run_capture_records_process_start_and_child_exit(self) -> None:
        runner = Runner(WriterLock())
        code, out, _ = runner.run_capture("t5", _child("print('x')"), input_text="", timeout=20)
        self.assertEqual((code, out.strip()), (0, "x"))
        self.assertLessEqual(runner.last_events["process_start"], runner.last_events["child_exit"])

    def test_run_capture_timeout_records_cleanup(self) -> None:
        runner = Runner(WriterLock())
        with self.assertRaises(subprocess.TimeoutExpired):
            runner.run_capture("t6", _child("import time; time.sleep(60)"), input_text="", timeout=1.0)
        self.assertIn("timeout", runner.last_events)
        self.assertIn("cleanup_end", runner.last_events)


if __name__ == "__main__":
    unittest.main()
