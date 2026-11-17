"""Durable chat admission across providers, task ids and a real host death."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sophia.runtime.execution_contract import ExecutionContract
from sophia.runtime.execution_recovery import ExecutionJournal
from .chat_adapter import ChatAdapterError, ClaudeChatAdapter, CodexChatAdapter
from .chat_recovery import CHAT_RECOVERY_KEY
from .chat_session import ChatSession
from .usage import TokenUsage

FLAGS = {"SOPHIA_G2_DEADLINE_CONTRACT": "1", "SOPHIA_G2_CHAT_RECOVERY": "1"}


class LocalRunner:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = []
        self.last_events = {}

    def run_capture(self, *args, **kwargs):
        self.calls.append(kwargs)
        self.last_events = {"process_start": time.monotonic()}
        if self.mode in ("timeout", "recovered"):
            self.last_events["cleanup_end"] = time.monotonic()
            output = '{"type":"result","result":"done"}' if self.mode == "recovered" else ""
            raise subprocess.TimeoutExpired("local-double", 1, output=output)
        self.last_events["child_exit"] = time.monotonic()
        if self.mode == "relay":
            return 0, json.dumps({"type": "result", "result": "Codex에게: 다음 작업"}), ""
        return (1, '{"type":"error","message":"quota reached"}', "") if self.mode == "error" else (
            0, '{"type":"result","result":"OK"}', "")


class ChatRecoveryTests(unittest.TestCase):
    def adapter(self, root, runner, cls=ClaudeChatAdapter):
        return cls(runner, cwd=str(root), timeout_seconds=60, executable_path="local-double",
                   show_progress=False)

    def record_path(self, root):
        return Path(root) / "workspace/runtime/live_chat" / CHAT_RECOVERY_KEY / "execution.json"

    def record(self, root):
        return json.loads(self.record_path(root).read_text(encoding="utf-8"))

    def test_off_preserves_existing_path_and_creates_no_journal(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOPHIA_G2_CHAT_RECOVERY": "0"}):
            runner = LocalRunner()
            self.assertEqual(self.adapter(tmp, runner).send("chat-claude", "hi"), "OK")
            self.assertFalse((Path(tmp) / "workspace").exists())

    def test_requires_shared_deadline_flag_before_writing_or_calling(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS | {"SOPHIA_G2_DEADLINE_CONTRACT": "0"}):
            runner = LocalRunner()
            with self.assertRaisesRegex(ChatAdapterError, "requires"):
                self.adapter(tmp, runner).send("chat-claude", "hi")
            self.assertFalse(runner.calls)
            self.assertFalse((Path(tmp) / "workspace").exists())

    def test_clean_sends_archive_the_prior_generation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            runner = LocalRunner()
            adapter = self.adapter(tmp, runner)
            for _ in range(2):
                self.assertEqual(adapter.send("chat-claude", "hi"), "OK")
            self.assertEqual(self.record(tmp)["status"], "FINISHED")
            self.assertEqual(self.record(tmp)["generation"], 2)
            self.assertTrue(self.record_path(tmp).with_name("execution.g1.json").is_file())

    def test_timeout_blocks_same_provider_and_a_different_provider_and_task_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            with self.assertRaises(ChatAdapterError):
                self.adapter(tmp, LocalRunner("timeout")).send("chat-claude", "first")
            self.assertEqual(self.record(tmp)["status"], "NEEDS_RECONCILIATION")
            for cls, task_id in ((ClaudeChatAdapter, "new-task"), (CodexChatAdapter, "chat-codex")):
                runner = LocalRunner()
                with self.assertRaisesRegex(ChatAdapterError, "blocked"):
                    self.adapter(tmp, runner, cls).send(task_id, "different request")
                self.assertFalse(runner.calls)

    def test_operator_reconciliation_releases_only_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            with self.assertRaises(ChatAdapterError):
                self.adapter(tmp, LocalRunner("timeout")).send("chat-claude", "hi")
            journal = ExecutionJournal(Path(tmp) / "workspace/runtime/live_chat")
            journal.reconcile(CHAT_RECOVERY_KEY, "inspected child termination and workspace")
            self.assertEqual(self.adapter(tmp, LocalRunner()).send("chat-claude", "new request"), "OK")
            self.assertEqual(self.record(tmp)["generation"], 2)
            previous = json.loads(self.record_path(tmp).with_name("execution.g1.json").read_text(encoding="utf-8"))
            self.assertEqual(previous["terminal"], "RECONCILED")

    def test_blocked_admission_does_not_reuse_previous_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            adapter = self.adapter(tmp, LocalRunner("timeout"))
            with self.assertRaises(ChatAdapterError):
                adapter.send("chat-claude", "hi")
            adapter.last_usage = TokenUsage(input_tokens=99)
            adapter.last_reset = True
            with self.assertRaises(ChatAdapterError):
                adapter.send("chat-claude", "again")
            self.assertIsNone(adapter.last_timing)
            self.assertIsNone(adapter.last_usage)
            self.assertIsNone(adapter.last_completion_report)
            self.assertFalse(adapter.last_reset)

    def test_both_does_not_start_second_provider_after_unsettled_execution(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            first, second = LocalRunner("timeout"), LocalRunner()
            session = ChatSession({"Claude": self.adapter(tmp, first),
                                   "Codex": self.adapter(tmp, second, CodexChatAdapter)},
                                  default_agent="Both", printer=lambda *_: None,
                                  attachment_root=Path(tmp), show_timing=False, show_usage=False)
            session.handle_line("hi")
            self.assertEqual(len(first.calls), 1)
            self.assertFalse(second.calls)
            self.assertEqual(self.record(tmp)["status"], "NEEDS_RECONCILIATION")

    def test_relay_keeps_shared_deadline_and_archives_settled_send(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            first, second = LocalRunner("relay"), LocalRunner()
            session = ChatSession({"Claude": self.adapter(tmp, first),
                                   "Codex": self.adapter(tmp, second, CodexChatAdapter)},
                                  printer=lambda *_: None, attachment_root=Path(tmp),
                                  show_timing=False, show_usage=False)
            session.handle_line("hi")
            self.assertEqual((len(first.calls), len(second.calls)), (1, 1))
            self.assertLessEqual(second.calls[0]["deadline_at"], first.calls[0]["deadline_at"])
            self.assertEqual(self.record(tmp)["status"], "FINISHED")
            self.assertEqual(self.record(tmp)["generation"], 2)

    def test_nonzero_exit_is_settled_but_still_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            with self.assertRaisesRegex(ChatAdapterError, "quota reached"):
                self.adapter(tmp, LocalRunner("error")).send("chat-claude", "hi")
            self.assertEqual(self.record(tmp)["status"], "FINISHED")
            self.assertEqual(self.record(tmp)["terminal"], "FAILED")
            self.assertTrue(self.record(tmp)["attempts"][0]["settled"])

    def test_returning_a_recovered_result_does_not_clear_unverified_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            self.assertEqual(self.adapter(tmp, LocalRunner("recovered")).send("chat-claude", "hi"), "done")
            self.assertEqual(self.record(tmp)["status"], "NEEDS_RECONCILIATION")

    def test_inherited_deadline_is_never_extended(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            runner = LocalRunner()
            contract = ExecutionContract.start("live_chat", "chat-claude", "parent", 2)
            self.adapter(tmp, runner).send("chat-claude", "hi", execution_contract=contract)
            self.assertLessEqual(runner.calls[0]["deadline_at"], contract.deadline_at)
            self.assertLessEqual(runner.calls[0]["timeout"], 2)

    def test_wrong_or_expired_parent_cannot_start_or_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            runner = LocalRunner()
            adapter = self.adapter(tmp, runner)
            wrong = ExecutionContract.start("agent_supervisor", "chat-claude", "parent", 2)
            with self.assertRaises(ValueError):
                adapter.send("chat-claude", "hi", execution_contract=wrong)
            expired = ExecutionContract("live_chat", "chat-claude", "parent", time.monotonic()-2, time.monotonic()-1)
            with self.assertRaises(TimeoutError):
                adapter.send("chat-claude", "hi", execution_contract=expired)
            self.assertFalse(runner.calls)
            self.assertFalse((Path(tmp) / "workspace").exists())

    def test_existing_live_owner_or_unreadable_record_blocks_execution(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            journal = ExecutionJournal(Path(tmp) / "workspace/runtime/live_chat")
            journal.open("live_chat", CHAT_RECOVERY_KEY, "live-owner", 60)
            runner = LocalRunner()
            with self.assertRaisesRegex(ChatAdapterError, "live process"):
                self.adapter(tmp, runner).send("chat-claude", "hi")
            self.record_path(tmp).write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ChatAdapterError, "unreadable"):
                self.adapter(tmp, runner).send("chat-claude", "hi")
            self.assertFalse(runner.calls)

    def test_real_host_death_after_admission_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
            child = """
import os,sys
from scripts.supervisor.chat_adapter import ClaudeChatAdapter
class Runner:
 def run_capture(self,*args,**kwargs): os._exit(7)
ClaudeChatAdapter(Runner(),cwd=sys.argv[1],executable_path='local-double',show_progress=False).send('chat-claude','hi')
"""
            process = subprocess.run([sys.executable, "-c", child, tmp], timeout=15, capture_output=True)
            self.assertEqual(process.returncode, 7, process.stderr)
            runner = LocalRunner()
            with self.assertRaisesRegex(ChatAdapterError, "interrupted"):
                self.adapter(tmp, runner).send("chat-claude", "hi")
            self.assertFalse(runner.calls)
            self.assertEqual(self.record(tmp)["status"], "NEEDS_RECONCILIATION")
