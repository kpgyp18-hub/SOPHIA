"""State mapping S2: journal attempts carry the tracker task id, and an unsettled
or blocked execution holds the tracker task instead of completing it."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .chat_adapter import ClaudeChatAdapter, CodexChatAdapter
from .chat_session import ChatSession
from .test_effect_evidence import FLAGS, LocalRunner
from sophia.runtime.execution_recovery import ExecutionJournal, ReconciliationRequired
from sophia.runtime.state_projection import project_status
from sophia.runtime.store import TaskStore
from sophia.runtime.tracker import ChatTaskTracker


class JournalTaskRefTests(unittest.TestCase):
    def test_task_ref_is_optional_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = ExecutionJournal(tmp, owner_alive=lambda pid: False)
            journal.open("live_chat", "task", "a1", 60)
            journal.attempt_started("task", "a1", "chat-claude", task_ref="task-0123456789ab")
            journal.attempt_started("task", "a2", "chat-claude")  # legacy shape still accepted
            items = journal.load("task")["attempts"]
            self.assertEqual(items[0]["task_ref"], "task-0123456789ab")
            self.assertNotIn("task_ref", items[1])
            for bad in ("", "x" * 65, "../etc", "a b", 7):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    journal.attempt_started("task", f"bad-{bad!r}", "chat-claude", task_ref=bad)
            self.assertEqual(len(journal.load("task")["attempts"]), 2)  # rejected before mutation

    def test_malformed_task_ref_on_disk_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = ExecutionJournal(tmp, owner_alive=lambda pid: False)
            journal.open("live_chat", "task", "a1", 60)
            journal.attempt_started("task", "a1", "chat-claude", task_ref="task-1")
            path = journal.path("task")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["attempts"][0]["task_ref"] = "../../escape"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises((ReconciliationRequired, ValueError)):
                journal.load("task")


class TrackerHoldTests(unittest.TestCase):
    def _session(self, tmp, runner):
        tracker = ChatTaskTracker("test", TaskStore(Path(tmp) / "tasks"))
        adapter = ClaudeChatAdapter(runner, cwd=tmp, executable_path="fixture", show_progress=False)
        # ChatSession expects both agents; Codex is never addressed in these tests.
        codex = CodexChatAdapter(LocalRunner(), cwd=tmp, executable_path="fixture", show_progress=False)
        session = ChatSession({"Claude": adapter, "Codex": codex}, default_agent="Claude", attachment_root=Path(tmp),
                              printer=lambda *args: None, task_tracker=tracker, auto_relay_enabled=False)
        return session, tracker

    @staticmethod
    def _journal(tmp):
        return ExecutionJournal(Path(tmp) / "workspace/runtime/live_chat")

    @staticmethod
    def _latest(tracker):
        return tracker.store.recent(1)[0]

    def test_settled_send_links_task_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS), \
                patch("scripts.supervisor.chat_adapter.ClaudeChatAdapter._resolve_binary", return_value="fixture"):
            session, tracker = self._session(tmp, LocalRunner("ok"))
            session.handle_line("@claude hello")
            task = self._latest(tracker)
            record = self._journal(tmp).load("live-chat")
            self.assertEqual(record["attempts"][0]["task_ref"], task["id"] if "id" in task else task["task_id"])
            self.assertNotEqual(task["status"], "user_decision_required")
            self.assertTrue(project_status(record, task["status"])["consistent"])

    def test_unsettled_then_blocked_sends_hold_the_tracker_task(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS), \
                patch("scripts.supervisor.chat_adapter.ClaudeChatAdapter._resolve_binary", return_value="fixture"):
            runner = LocalRunner("partial")
            session, tracker = self._session(tmp, runner)
            session.handle_line("@claude long work")
            first = self._latest(tracker)
            self.assertEqual(first["status"], "user_decision_required")
            self.assertEqual(first["events"][-1]["type"], "execution_hold")
            self.assertIn("needs reconciliation", first["events"][-1]["reason"])
            record = self._journal(tmp).load("live-chat")
            view = project_status(record, first["status"])
            self.assertEqual((view["status"], view["consistent"]), ("NEEDS_RECONCILIATION", True))

            session.handle_line("@claude try again")
            self.assertEqual(runner.calls, 1)  # blocked: no second process
            second = self._latest(tracker)
            self.assertNotEqual(second.get("id", second.get("task_id")), first.get("id", first.get("task_id")))
            self.assertEqual(second["status"], "user_decision_required")
            self.assertIn("blocked", second["events"][-1]["reason"])

            self._journal(tmp).reconcile("live-chat", "fixture inspected")
            runner.mode = "ok"
            session.handle_line("@claude after reconcile")
            third = self._latest(tracker)
            self.assertNotEqual(third["status"], "user_decision_required")  # hold does not leak

    def test_flag_off_path_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOPHIA_G2_CHAT_RECOVERY": "0"}), \
                patch("scripts.supervisor.chat_adapter.ClaudeChatAdapter._resolve_binary", return_value="fixture"):
            session, tracker = self._session(tmp, LocalRunner("partial"))
            session.handle_line("@claude long work")
            self.assertFalse((Path(tmp) / "workspace/runtime/live_chat").exists())
            self.assertNotEqual(self._latest(tracker)["status"], "user_decision_required")


if __name__ == "__main__":
    unittest.main()
