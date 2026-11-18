"""Regression cases R4/R5/R7/R9 from the Claude implementation handoff."""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from .chat_adapter import ClaudeChatAdapter, CodexChatAdapter
from .chat_session import ChatSession
from .long_task import pid_alive


class LocalRunner:
    def __init__(self):
        self.calls = 0
        self.last_events = {}

    def run_capture(self, *args, **kwargs):
        self.calls += 1
        self.last_events = {"process_start": time.monotonic(), "child_exit": time.monotonic()}
        return 0, '{"type":"result","result":"reply","usage":{"input_tokens":2,"output_tokens":1}}', ""


class CheckpointFixTests(unittest.TestCase):
    def test_corrupt_timestamp_or_unhashable_pending_refuses_resume_without_calls(self):
        flags = {"SOPHIA_G2_MESSAGE_RECOVERY": "1", "SOPHIA_G2_CHAT_RECOVERY": "1", "SOPHIA_G2_DEADLINE_CONTRACT": "1"}
        for corruption in ("at", "pending"):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, flags):
                runner = LocalRunner()
                adapter = ClaudeChatAdapter(runner, cwd=tmp, executable_path="fixture", show_progress=False)
                output = []
                other = CodexChatAdapter(runner, cwd=tmp, executable_path="fixture", show_progress=False)
                session = ChatSession({"Claude": adapter, "Codex": other}, attachment_root=Path(tmp), default_agent="Claude",
                                      printer=lambda *args: output.append(args))
                session.handle_line("/request stable @claude hello")
                path = Path(tmp) / "workspace/runtime/live_chat/live-chat/execution.json"
                record = json.loads(path.read_text(encoding="utf-8"))
                snapshot = record["request"]["checkpoint"]
                if corruption == "at":
                    snapshot["history"][0]["at"] = 123
                else:
                    snapshot["pending"] = [[]]
                path.write_text(json.dumps(record), encoding="utf-8")
                runner.calls = 0
                output.clear()
                self.assertTrue(session.handle_line("/resume stable"))
                self.assertEqual(runner.calls, 0)
                self.assertTrue(any("malformed" in text for _, text in output))

    def test_disabled_request_and_resume_have_exactly_one_notice(self):
        for command in ("/request id hello", "/resume id"):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOPHIA_G2_MESSAGE_RECOVERY": "0"}):
                runner = LocalRunner()
                adapter = ClaudeChatAdapter(runner, cwd=tmp, executable_path="fixture", show_progress=False)
                output = []
                session = ChatSession({"Claude": adapter}, attachment_root=Path(tmp), printer=lambda *args: output.append(args))
                session.handle_line(command)
                self.assertEqual(output, [("System", "message recovery is disabled")])
                self.assertEqual(runner.calls, 0)

    def test_usage_log_oserror_does_not_invalidate_completed_send(self):
        adapter = ClaudeChatAdapter(LocalRunner(), cwd=".", executable_path="fixture", show_progress=False)
        with patch.object(adapter.usage_log, "record", side_effect=OSError("disk full")):
            self.assertEqual(adapter.send("task", "hello"), "reply")
        self.assertEqual((adapter.last_timing.outcome, adapter.last_timing.exit_code), ("completed", 0))
        self.assertEqual(adapter.last_usage_log_error, "disk full")
        self.assertEqual(adapter.send("task", "next"), "reply")
        self.assertIsNone(adapter.last_usage_log_error)

    def test_current_and_reaped_process_liveness(self):
        self.assertTrue(pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.assertFalse(pid_alive(child.pid))

    @unittest.skipUnless(os.name == "nt", "Windows last-error API")
    def test_unknown_or_denied_process_query_is_not_proof_of_death(self):
        kernel = SimpleNamespace(OpenProcess=Mock(return_value=None), GetExitCodeProcess=Mock(), CloseHandle=Mock())
        for error, expected in ((5, True), (0, True), (87, False)):
            with patch.object(ctypes, "WinDLL", return_value=kernel) as loader, patch.object(ctypes, "get_last_error", return_value=error):
                self.assertEqual(pid_alive(12345), expected)
                loader.assert_called_once_with("kernel32", use_last_error=True)
