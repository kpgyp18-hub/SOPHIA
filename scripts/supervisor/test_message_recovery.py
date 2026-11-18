"""Durable message identity and real host-death checkpoints; no model calls."""
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

from .chat_adapter import ClaudeChatAdapter, CodexChatAdapter
from .chat_session import ChatSession

FLAGS = {"SOPHIA_G2_DEADLINE_CONTRACT": "1", "SOPHIA_G2_CHAT_RECOVERY": "1",
         "SOPHIA_G2_MESSAGE_RECOVERY": "1"}
ROOT = Path(__file__).resolve().parents[2]


class LocalRunner:
    def __init__(self, reply="saved reply"):
        self.reply = reply
        self.calls = []
        self.last_events = {}
    def run_capture(self, task_id, command, **kwargs):
        self.calls.append(kwargs)
        self.last_events = {"process_start": time.monotonic(), "child_exit": time.monotonic()}
        return 0, json.dumps({"type": "result", "result": self.reply}), ""


CHILD = r'''
import json,os,sys,time
from pathlib import Path
from scripts.supervisor.chat_adapter import ClaudeChatAdapter,CodexChatAdapter
from scripts.supervisor.chat_session import ChatSession
from sophia.runtime.execution_recovery import ExecutionJournal
stage=sys.argv[2]
class Runner:
 def run_capture(self,*args,**kwargs):
  if stage=='inflight': os._exit(7)
  self.last_events={'process_start':time.monotonic(),'child_exit':time.monotonic()}
  return 0,json.dumps({'type':'result','result':'durably saved first answer'}),''
original=ExecutionJournal.checkpoint
def checkpoint(self,key,snapshot):
 original(self,key,snapshot)
 stop=(stage=='initial' and not self.load(key)['attempts']) or (stage=='between' and snapshot['pending']==['Codex']) or (stage=='final' and snapshot['complete'])
 if stop: os._exit(7)
ExecutionJournal.checkpoint=checkpoint
root=Path(sys.argv[1]);agents={'Claude':ClaudeChatAdapter(Runner(),cwd=str(root),timeout_seconds=30,executable_path='local-double',show_progress=False),'Codex':CodexChatAdapter(Runner(),cwd=str(root),timeout_seconds=30,executable_path='local-double',show_progress=False)}
session=ChatSession(agents,default_agent='Both',attachment_root=root,printer=lambda *_:None)
if stage=='gap': session._complete_request=lambda *args:os._exit(7)
session.handle_line('/request stable @both original request')
'''


class MessageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.flags = patch.dict(os.environ, FLAGS)
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def session(self, first="first reply", second="second reply"):
        self.runners = {"Claude": LocalRunner(first), "Codex": LocalRunner(second)}
        agents = {"Claude": ClaudeChatAdapter(self.runners["Claude"], cwd=str(self.root), executable_path="local-double", show_progress=False),
                  "Codex": CodexChatAdapter(self.runners["Codex"], cwd=str(self.root), executable_path="local-double", show_progress=False)}
        self.output = []
        return ChatSession(agents, default_agent="Both", attachment_root=self.root,
                           printer=lambda *args:self.output.append(args), show_timing=False, show_usage=False)

    def record(self):
        return json.loads((self.root / "workspace/runtime/live_chat/live-chat/execution.json").read_text(encoding="utf-8"))

    def crash(self, stage):
        result = subprocess.run([sys.executable, "-c", CHILD, str(self.root), stage], cwd=ROOT,
                                env=dict(os.environ, **FLAGS), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_completed_request_and_older_archive_are_reused_without_calls_or_duplicate_history(self):
        session = self.session()
        session.handle_line("/request one @both hello")
        history_size = len(session.history)
        session.handle_line("/request one @both hello")
        self.assertEqual([len(r.calls) for r in self.runners.values()], [1, 1])
        self.assertEqual(len(session.history), history_size)
        session.handle_line("/request two @both next")
        session.handle_line("/request one @both hello")
        self.assertEqual([len(r.calls) for r in self.runners.values()], [2, 2])
        self.assertTrue(any("reusing saved result" in text for _,text in self.output))

    def test_reused_id_rejects_changed_content_or_route_before_recording(self):
        session = self.session()
        session.handle_line("/request stable @claude hello")
        size = len(session.history)
        session.handle_line("/request stable @claude changed")
        session.handle_line("/request stable @codex hello")
        self.assertEqual([len(r.calls) for r in self.runners.values()], [1, 0])
        self.assertEqual(len(session.history), size)

    def test_both_is_one_generation_with_checkpointed_attempts(self):
        self.session().handle_line("/request both @both hello")
        record = self.record()
        self.assertEqual(record["generation"], 1)
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(record["request"]["checkpoint_attempts"], 2)
        self.assertTrue(record["request"]["checkpoint"]["complete"])

    def test_real_restart_skips_checkpointed_first_provider_and_restores_history_with_remaining_budget(self):
        self.crash("between")
        session = self.session()
        session.handle_line("/resume stable")
        self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 1])
        call = self.runners["Codex"].calls[0]
        self.assertIn("durably saved first answer", call["input_text"])
        self.assertLess(call["deadline_at"] - time.monotonic(), 30)
        self.assertEqual(self.record()["takeovers"], 1)
        self.assertEqual(self.record()["status"], "FINISHED")

    def test_inflight_or_settled_but_uncheckpointed_work_never_reexecutes(self):
        for stage in ("inflight", "gap"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                prior = self.root
                self.root = Path(tmp)
                try:
                    self.crash(stage)
                    session = self.session()
                    session.handle_line("/resume stable")
                    session.handle_line("/request another @both different")
                    self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 0])
                    self.assertEqual(self.record()["status"], "NEEDS_RECONCILIATION")
                finally:
                    self.root = prior

    def test_final_checkpoint_is_recovered_even_if_deadline_has_passed(self):
        self.crash("final")
        path = self.root / "workspace/runtime/live_chat/live-chat/execution.json"
        record = self.record()
        record["started_wall"] -= 1000
        path.write_text(json.dumps(record), encoding="utf-8")
        self.session().handle_line("/resume stable")
        self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 0])
        self.assertEqual(self.record()["terminal"], "DONE")

    def test_expired_initial_checkpoint_starts_nothing(self):
        self.crash("initial")
        path = self.root / "workspace/runtime/live_chat/live-chat/execution.json"
        record = self.record()
        record["started_wall"] -= 1000
        path.write_text(json.dumps(record), encoding="utf-8")
        self.session().handle_line("/resume stable")
        self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 0])
        self.assertEqual(self.record()["terminal"], "EXPIRED")

    def test_missing_prerequisites_or_oversized_payload_create_no_execution(self):
        session = self.session()
        with patch.dict(os.environ, {"SOPHIA_G2_CHAT_RECOVERY": "0"}):
            session.handle_line("/request no @both hello")
        session.handle_line("/request large @both " + "x" * 300000)
        self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 0])
        self.assertFalse((self.root / "workspace").exists())

    def test_relay_is_checkpointed_and_obeys_original_hop_bound(self):
        session = self.session(first="Codex에게: continue", second="Claude에게: review")
        session.handle_line("/request relay @claude hello")
        self.assertEqual(sum(len(r.calls) for r in self.runners.values()), 5)
        self.assertEqual(self.record()["request"]["checkpoint"]["hops"], 4)

    def test_restore_completed_request_rehydrates_bounded_context_without_replaying(self):
        self.session().handle_line("/request complete @both hello")
        new = self.session()
        new.handle_line("/resume complete")
        self.assertEqual([turn.speaker for turn in new.history], ["You", "Claude", "Codex"])
        self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 0])

    def test_tracker_completion_is_idempotent_and_hold_is_not_left_executing(self):
        from sophia.runtime.tracker import ChatTaskTracker
        from sophia.runtime.store import TaskStore
        tracker = ChatTaskTracker("test", TaskStore(self.root / "observed-tasks"))
        task_id = tracker.begin("hello")
        tracker.hold(task_id, "unsettled")
        tracker.hold(task_id, "unsettled")
        state = tracker.store.read(task_id)
        self.assertEqual(state["status"], "user_decision_required")
        self.assertEqual(sum(e["type"] == "execution_hold" for e in state["events"]), 1)
        # State mapping S3: a held task is terminal under Core's state machine,
        # so completion must not overwrite it. Idempotency is checked on a fresh task.
        tracker.complete(task_id)
        self.assertEqual(tracker.store.read(task_id)["status"], "user_decision_required")
        completed_id = tracker.begin("hello")
        tracker.complete(completed_id)
        tracker.complete(completed_id)
        state = tracker.store.read(completed_id)
        self.assertEqual(sum(e["type"] == "completed" for e in state["events"]), 1)

    def test_refused_duplicate_keeps_pending_attachments(self):
        session = self.session()
        session.handle_line("/request same @both hello")
        attachment = self.root / "data.txt"
        attachment.write_text("example")
        session._pending_attachments = [attachment]
        session.handle_line("/request same @both changed")
        self.assertEqual(session._pending_attachments, [attachment])
        self.assertEqual([len(r.calls) for r in self.runners.values()], [1, 1])

    def test_cached_payload_drift_is_refused_before_restoring_history(self):
        self.session().handle_line("/request cached @both hello")
        path = self.root / "workspace/runtime/live_chat/live-chat/execution.json"
        record = self.record()
        record["request"]["checkpoint"]["payload"]["message"] = "altered"
        path.write_text(json.dumps(record), encoding="utf-8")
        session = self.session()
        session.handle_line("/request cached @both hello")
        self.assertFalse(session.history)
        self.assertEqual([len(r.calls) for r in self.runners.values()], [0, 0])

    def test_failed_message_is_held_in_tracker_instead_of_completed(self):
        from sophia.runtime.tracker import ChatTaskTracker
        from sophia.runtime.store import TaskStore
        session = self.session()
        tracker = ChatTaskTracker("test", TaskStore(self.root / "observed-tasks"))
        session.task_tracker = tracker
        original = self.runners["Claude"].run_capture
        def failure(*args, **kwargs):
            original(*args, **kwargs)
            return 1, '{"type":"error","message":"quota reached"}', ""
        self.runners["Claude"].run_capture = failure
        session.handle_line("/request failed @both hello")
        record = self.record()
        self.assertEqual(record["terminal"], "FAILED")
        state = tracker.store.read(record["request"]["checkpoint"]["tracker_task_id"])
        self.assertEqual(state["status"], "user_decision_required")
