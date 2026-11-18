"""Progress observations, bounded state and report compatibility; no providers."""
from __future__ import annotations

import hashlib
import json
import pickle
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import timing as timing_module
from .chat_adapter import ClaudeChatAdapter
from .runner import Runner
from .stream_text import classify_progress
from .timing import ProgressStats, TimingLog
from .writer_lock import WriterLock
from scripts.timing_report import summarize
from sophia.runtime.execution_contract import ExecutionContract


def tool(identifier, ending=False):
    return json.dumps({"type": "item.completed" if ending else "item.started",
                       "item": {"id": identifier, "type": "command_execution"}})


class ProgressStatsTests(unittest.TestCase):
    def test_classifies_only_verified_shapes(self):
        for event, expected in (
            ({"type": "thread.started"}, "meta"), ({"type": "system", "subtype": "init"}, "meta"),
            ({"type": "turn.completed"}, "terminal"), ({"type": "result"}, "terminal"),
            ({"type": "item.completed", "item": {"type": "agent_message"}}, "text"),
            ({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "hi"}}}, "text"),
            ({"type": "stream_event", "event": {"delta": {"type": "input_json_delta", "partial_json": "{}"}}}, "unknown"),
            ({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}, "unknown"),
            ({"type": "item.completed", "item": {"type": "file_change"}}, "unknown"),
            ({"type": ["malformed"]}, "unknown"), ([], "unknown"),
        ):
            self.assertEqual(classify_progress(json.dumps(event)), expected)
        self.assertEqual(classify_progress("not json"), "unknown")
        self.assertEqual(classify_progress(tool("a")), "tool_start")
        self.assertEqual(classify_progress(tool("a", True)), "tool_end")

    def test_recorded_l1_replay_without_ignored_workspace_dependency(self):
        # Recorded timestamp pairs, no original command strings or user text.
        intervals = [(9.388, 9.454), (9.402, 9.773), (24.486, 24.728), (24.501, 24.546),
                     (46.922, 47.118), (46.938, 47.126), (76.484, 76.733), (84.536, 84.855),
                     (92.502, 92.830), (101.451, 101.741), (109.456, 109.728), (123.055, 123.255),
                     (133.156, 133.437), (145.640, 145.874), (152.896, 153.145),
                     (160.351, 160.572), (212.015, 212.203)]
        replay = [(0.296, '{"type":"thread.started"}'), (0.527, '{"type":"turn.started"}')]
        for at in (4.649, 73.080, 119.764, 247.395):
            replay.append((at, '{"type":"item.completed","item":{"type":"agent_message","text":"fixture"}}'))
        for identifier, (start, end) in enumerate(intervals):
            replay += [(start, tool(str(identifier))), (end, tool(str(identifier), True))]
        stats = ProgressStats(0)
        for at, line in sorted(replay):
            stats.observe(line, at)
        result = stats.snapshot(305.205, {"process_start": 0, "stdin_closed": 0.01, "timeout": 300.041}, "timeout")
        self.assertEqual(result["stdout_lines"], 40)
        self.assertEqual(result["kinds"], {"meta": 2, "text": 4, "tool_start": 17, "tool_end": 17, "terminal": 0, "unknown": 0})
        self.assertEqual(result["tool_count"], 17)
        self.assertAlmostEqual(result["tool_time_s"], 3.939, places=3)
        self.assertEqual(result["tools_open_at_end"], 0)
        self.assertEqual(result["last_line_s"], 247.395)
        self.assertEqual(result["tail_idle_s"], 52.646)
        self.assertEqual(result["longest_gap_s"], 52.646)
        self.assertEqual(result["longest_gap_start_s"], 247.395)
        self.assertEqual(result["timeout_phase"], "silent_after_progress")

    def test_each_timeout_phase_and_exact_idle_boundary(self):
        events = {"process_start": 0, "stdin_closed": 0.01}
        for extra, at, end, expected in (
            ([], None, 10, "no_output"), ([tool("open")], 1, 10, "tool_running"),
            (['{"type":"result"}'], 1, 10, "terminal_seen_exit_hung"),
            (['{"type":"item.completed","item":{"type":"agent_message"}}'], 1, 6, "silent_after_progress"),
            (['{"type":"item.completed","item":{"type":"agent_message"}}'], 1, 5.999, "active_until_deadline"),
        ):
            stats = ProgressStats(0)
            for line in extra:
                stats.observe(line, at)
            self.assertEqual(stats.snapshot(end, events, "timeout")["timeout_phase"], expected)
        self.assertEqual(ProgressStats(0).snapshot(10, {}, "timeout")["timeout_phase"], "before_start")
        self.assertEqual(ProgressStats(0).snapshot(10, {"process_start": 0}, "timeout")["timeout_phase"], "input")
        self.assertIsNone(ProgressStats(0).snapshot(10, events, "completed")["timeout_phase"])

    def test_constant_state_with_100000_unique_tools_and_large_ids(self):
        stats = ProgressStats(0)
        for index in range(100000):
            stats.observe(tool(str(index)), 1)
        result = stats.snapshot(2, {"process_start": 0, "stdin_closed": 0}, "timeout")
        self.assertEqual(result["stdout_lines"], 100000)
        self.assertEqual(result["tools_open_at_end"], 64)
        self.assertEqual(result["tool_overflow"], 100000 - 64)
        self.assertLess(len(pickle.dumps(stats)), 12000)
        self.assertLess(len(json.dumps(result)), 3000)
        bounded = ProgressStats(0)
        bounded.observe(tool("x" * 1000000), 1)
        self.assertEqual(bounded.tool_overflow, 1)
        self.assertLess(len(pickle.dumps(bounded)), 2000)

    def test_nonstreaming_counts_have_no_invented_times_and_snapshot_is_repeatable(self):
        stats = ProgressStats(0, streaming=False)
        stdout = tool("a") + "\n" + tool("a", True) + '\n{"type":"result"}'
        first = stats.snapshot(100, {"process_start": 0, "stdin_closed": 1}, "timeout", stdout=stdout)
        second = stats.snapshot(100, {"process_start": 0, "stdin_closed": 1}, "timeout", stdout=stdout)
        self.assertEqual(first, second)
        self.assertEqual(first["stdout_lines"], 3)
        self.assertEqual(first["stdout_bytes"], len(stdout.encode()))
        for field in ("tool_time_s", "first_progress_s", "last_progress_s", "last_line_s", "tail_idle_s", "longest_gap_s"):
            self.assertIsNone(first[field])

    def test_meta_lines_do_not_hide_a_gap_between_meaningful_events(self):
        stats = ProgressStats(0)
        stats.observe('{"type":"item.completed","item":{"type":"agent_message"}}', 1)
        stats.observe('{"type":"system"}', 20)
        stats.observe('{"type":"system"}', 29)
        result = stats.snapshot(30, {"process_start": 0, "stdin_closed": 0}, "timeout")
        self.assertEqual(result["last_line_s"], 29)
        self.assertEqual(result["last_progress_s"], 1)
        self.assertEqual(result["longest_progress_gap_s"], 29)

    def test_tool_pairing_uses_ids_and_preserves_overlapping_durations(self):
        stats = ProgressStats(0)
        for line, at in ((tool("a"), 1), (tool("b"), 2), (tool("a"), 3),
                         (tool("missing", True), 4), (tool("b", True), 5), (tool("a", True), 6)):
            stats.observe(line, at)
        result = stats.snapshot(10, {"process_start": 0, "stdin_closed": 0}, "completed")
        self.assertEqual(result["tool_time_s"], 8)  # b:3 + a:5; duplicate start did not reset a
        self.assertEqual(result["tools_open_at_end"], 0)
        self.assertEqual(result["unmatched_tool_ends"], 1)


class LocalRunner:
    stdout = '{"type":"system","subtype":"init"}\n{"type":"result","result":"reply"}\n'

    def __init__(self):
        self.last_events = {}

    def run_capture(self, *args, **kwargs):
        self.last_events = {"process_start": time.monotonic(), "stdin_closed": time.monotonic()}
        return 0, self.stdout, ""

    def run_capture_streaming(self, *args, **kwargs):
        self.run_capture()
        for line in self.stdout.splitlines():
            kwargs["on_line"](line)
        self.last_events["child_exit"] = time.monotonic()
        return 0, self.stdout, ""


class AdapterProgressTests(unittest.TestCase):
    def adapter(self, runner=None, *, streaming=True):
        return ClaudeChatAdapter(runner or LocalRunner(), cwd=".", executable_path="fixture", show_progress=False,
                                 on_line=(lambda line: None) if streaming else None)

    def test_observer_or_classifier_exception_never_changes_the_response(self):
        for streaming in (True, False):
            for target in ("classify_progress", "ProgressStats.observe"):
                adapter = self.adapter(streaming=streaming)
                with patch.object(timing_module, "classify_progress", side_effect=RuntimeError("broken")) if target == "classify_progress" else patch.object(ProgressStats, "observe", side_effect=RuntimeError("broken")):
                    self.assertEqual(adapter.send("task", "hello"), "reply")
                result = adapter.last_timing.progress
                self.assertEqual(result["kinds"]["unknown"], 2)
                self.assertEqual(result["observation_errors"], 2)
                self.assertEqual((adapter.last_timing.outcome, adapter.last_timing.exit_code), ("completed", 0))

    def test_failed_prestart_does_not_reuse_any_previous_progress(self):
        adapter = self.adapter()
        adapter.send("task", "hello")
        self.assertEqual(adapter.last_timing.progress["stdout_lines"], 2)
        contract = ExecutionContract.start("live_chat", "task", "expired", 1, now=time.monotonic() - 10)
        with self.assertRaises(TimeoutError):
            adapter._run("task", ["fixture"], "hello", execution_contract=contract)
        result = adapter.last_timing.progress
        self.assertEqual(result["stdout_lines"], 0)
        self.assertEqual(result["timeout_phase"], "before_start")
        self.assertIsNone(result["last_line_s"])

    def test_counts_and_bytes_are_recorded_without_output_limit_flag(self):
        adapter = self.adapter(streaming=False)
        adapter.send("task", "hello")
        self.assertEqual(adapter.last_timing.progress["stdout_lines"], 2)
        self.assertEqual(adapter.last_timing.progress["stdout_bytes"], len(LocalRunner.stdout.encode()))
        self.assertIsNone(adapter.last_timing.progress["last_line_s"])

    def test_legacy_stream_runner_emits_input_consumption_and_persists_progress(self):
        runner = Runner(WriterLock())
        adapter = self.adapter(runner)
        command = [sys.executable, "-X", "utf8", "-c", 'import sys;sys.stdin.read();print(\'{"type":"result","result":"local"}\')']
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"SOPHIA_G2_BOUNDED_OUTPUT": "0"}):
            path = Path(tmp) / "timing.jsonl"
            adapter.timing_log = TimingLog(path)
            result = adapter._run("local", command, "input")
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["progress"], adapter.last_timing.progress)
        self.assertEqual(result[0], 0)
        self.assertIn("stdin_closed", adapter.last_timing.events)
        self.assertEqual(adapter.last_timing.progress["kinds"]["terminal"], 1)


class TimingReportTests(unittest.TestCase):
    def test_mixed_legacy_new_malformed_rows_and_read_only_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timing.jsonl"
            rows = [{"label": "Codex", "outcome": "timeout", "total_s": 300, "progress": {
                "timeout_phase": "silent_after_progress", "tail_idle_s": 52.646, "stdout_lines": 40}},
                {"label": "Claude", "outcome": "completed", "total_s": 2},
                {"label": "Codex", "outcome": ["malformed"], "progress": {"timeout_phase": [], "stdout_lines": -1}}, []]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\ninvalid\n", encoding="utf-8")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            report = summarize(path)
            self.assertEqual((report["rows"], report["legacy_rows"], report["malformed_rows"]), (3, 1, 2))
            group = next(row for row in report["groups"] if row["outcome"] == "timeout")
            self.assertEqual((group["timeout_phase"], group["max_tail_idle_s"], group["stdout_lines_max"]),
                             ("silent_after_progress", 52.646, 40))
            script = Path(__file__).resolve().parents[1] / "timing_report.py"
            run = subprocess.run([sys.executable, "-X", "utf8", str(script), str(path), "--json"], capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), report)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
