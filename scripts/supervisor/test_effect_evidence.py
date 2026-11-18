"""I07 evidence is bounded observation, never an admission or retry decision."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import effect_evidence as effects
from .chat_adapter import ChatAdapterError, ClaudeChatAdapter, CodexChatAdapter
from .chat_session import ChatSession
from sophia.runtime.execution_recovery import ExecutionJournal, ReconciliationRequired

ROOT = Path(__file__).resolve().parents[2]
FLAGS = {"SOPHIA_G2_CHAT_RECOVERY": "1", "SOPHIA_G2_DEADLINE_CONTRACT": "1"}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def axes(**changes):
    return {"process": "exited", "result": "final", "persisted": False,
            "effects": "none_observed", **changes}


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git(self.root, "init", "-q")
        (self.root / "tracked.txt").write_text("original")
        (self.root / "delete.txt").write_text("delete")
        (self.root / ".gitignore").write_text("*.ignored\n")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Local Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "baseline")

    def test_real_child_create_modify_delete_reports_paths(self):
        before = effects.workspace_fingerprint(self.root)
        child = ('from pathlib import Path;import sys;root=Path(sys.argv[1]);'
                 '(root/"created.txt").write_text("new");'
                 '(root/"tracked.txt").write_text("modified contents");'
                 '(root/"delete.txt").unlink()')
        run = subprocess.run([sys.executable, "-c", child, str(self.root)], capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        state, evidence = effects.compare_fingerprints(before, effects.workspace_fingerprint(self.root))
        self.assertEqual(state, "changed")
        self.assertEqual(evidence["paths"], ["created.txt", "delete.txt", "tracked.txt"])

    def test_clean_and_ignored_only_writes_are_scoped_none_observed_and_index_unchanged(self):
        index = self.root / ".git/index"
        saved = (index.read_bytes(), index.stat().st_mtime_ns)
        before = effects.workspace_fingerprint(self.root)
        (self.root / "hidden.ignored").write_text("an effect outside this observation scope")
        state, evidence = effects.compare_fingerprints(before, effects.workspace_fingerprint(self.root))
        self.assertEqual(state, "none_observed")
        self.assertIn("ignored", evidence["scope"])
        self.assertIn("DB/network", evidence["scope"])
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), saved)

    def test_already_dirty_path_metadata_detects_further_writes(self):
        path = self.root / "tracked.txt"
        path.write_text("first edit")
        before = effects.workspace_fingerprint(self.root)
        path.write_text("second larger edit")
        state, evidence = effects.compare_fingerprints(before, effects.workspace_fingerprint(self.root))
        self.assertEqual((state, evidence["paths"]), ("changed", ["tracked.txt"]))

    def test_rename_nul_format_and_subdirectory_paths_use_repository_root(self):
        folder = self.root / "nested"
        folder.mkdir()
        before = effects.workspace_fingerprint(folder)
        git(self.root, "mv", "tracked.txt", "renamed space.txt")
        state, evidence = effects.compare_fingerprints(before, effects.workspace_fingerprint(folder))
        self.assertEqual(state, "changed")
        self.assertEqual(evidence["paths"], ["renamed space.txt", "tracked.txt"])

    def test_paths_are_limited_and_unknown_snapshot_never_means_no_effect(self):
        before = effects.workspace_fingerprint(self.root)
        for index in range(55):
            (self.root / f"new{index:02}.txt").write_text("new")
        after = effects.workspace_fingerprint(self.root)
        state, evidence = effects.compare_fingerprints(before, after)
        self.assertEqual((state, len(evidence["paths"]), evidence["changed_paths_count"]), ("changed", 50, 55))
        self.assertTrue(evidence["truncated"])
        for setting, value in (("_MAX_STATUS_BYTES", 100), ("_MAX_PATHS", 2)):
            with patch.object(effects, setting, value):
                unknown = effects.workspace_fingerprint(self.root)
            self.assertEqual(unknown["status"], "unknown")
            self.assertEqual(effects.compare_fingerprints(before, unknown)[0], "unknown")

    def test_missing_git_timeout_and_invalid_budget_return_unknown(self):
        for failure in (FileNotFoundError("no git"), subprocess.TimeoutExpired("git", 0.01)):
            started = time.monotonic()
            with patch.object(effects.subprocess, "run", side_effect=failure):
                self.assertEqual(effects.workspace_fingerprint(self.root)["status"], "unknown")
            self.assertLess(time.monotonic() - started, 1)
        with patch.object(effects.subprocess, "run") as run:
            for value in (0, -1, float("inf"), True):
                self.assertEqual(effects.workspace_fingerprint(self.root, value)["status"], "unknown")
            run.assert_not_called()

    def test_real_slow_query_is_reaped_within_shared_budget(self):
        actual_run = subprocess.run
        def slow_query(command, **kwargs):
            return actual_run([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        started = time.monotonic()
        with patch.object(effects.subprocess, "run", side_effect=slow_query):
            observed = effects.workspace_fingerprint(self.root, timeout_s=0.1)
        self.assertEqual(observed["reason"], "git_observation_timeout")
        self.assertLess(time.monotonic() - started, 1.5)


class ObservationJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = ExecutionJournal(self.root, owner_alive=lambda pid: False)

    def start(self, request=False):
        kwargs = {"request_id": "request", "payload_hash": "0" * 64, "checkpoint": {}} if request else {}
        self.journal.open("live_chat", "task", "attempt", 60, **kwargs)
        self.journal.attempt_started("task", "attempt", "local")

    def test_optional_axes_keep_version_and_legacy_records_valid(self):
        self.start()
        self.journal.attempt_finished("task", "attempt", settled=True, detail="completed")
        self.assertNotIn("axes", self.journal.load("task")["attempts"][0])
        self.assertEqual(self.journal.close("task", "DONE"), "FINISHED")
        self.assertEqual(self.journal.load("task")["version"], 1)

    def test_evidence_does_not_settle_unverified_attempt_or_block_a_settled_one(self):
        self.start()
        self.journal.attempt_finished("task", "attempt", settled=False, detail="timeout", axes=axes())
        self.assertEqual(self.journal.close("task", "FAILED"), "NEEDS_RECONCILIATION")
        with self.assertRaises(ReconciliationRequired):
            ExecutionJournal(self.root, owner_alive=lambda pid: False).open("live_chat", "task", "again", 60)
        self.journal.reconcile("task", "inspected effects manually")
        self.journal.open("live_chat", "task", "second", 60)
        self.journal.attempt_started("task", "second", "local")
        self.journal.attempt_finished("task", "second", settled=True, detail="completed", axes=axes(effects="changed"))
        self.assertEqual(self.journal.close("task", "DONE"), "FINISHED")

    def test_axes_and_nonfinite_evidence_are_validated_before_mutation(self):
        self.start()
        before = self.journal.path("task").read_bytes()
        for invalid in (axes(process="alive"), axes(persisted=1), {}, axes(effects="proven_safe")):
            with self.assertRaises(ValueError):
                self.journal.attempt_finished("task", "attempt", settled=True, detail="done", axes=invalid)
        for invalid in ({"bad": float("nan")}, {"bad": object()}):
            with self.assertRaises(ValueError):
                self.journal.attempt_finished("task", "attempt", settled=True, detail="done", evidence=invalid)
        self.assertEqual(self.journal.path("task").read_bytes(), before)

    def test_oversized_evidence_is_truncated_and_reconciliation_keeps_seen_evidence(self):
        self.start()
        evidence = {"scope": effects.SCOPE, "paths": [f"path{index}-" + "x" * 1000 for index in range(300)]}
        self.journal.attempt_finished("task", "attempt", settled=False, detail="unverified", axes=axes(), evidence=evidence)
        item = self.journal.load("task")["attempts"][0]
        self.assertTrue(item["evidence"]["truncated"])
        self.assertLessEqual(len(json.dumps(item["evidence"], ensure_ascii=False).encode()), 16384)
        self.journal.close("task", "FAILED")
        record = self.journal.reconcile("task", "read the bounded effect observations")
        summary = record["reconciliations"][-1]["evidence_seen"]
        self.assertEqual(summary["attempts"][0]["axes"]["process"], "exited")
        self.assertEqual(record["attempts"][0], item)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False).encode()), 16384)

    def test_persisted_becomes_true_only_with_successful_result_checkpoint(self):
        self.start(request=True)
        self.journal.attempt_finished("task", "attempt", settled=True, detail="completed", axes=axes())
        with self.assertRaises(ValueError):
            self.journal.checkpoint("task", {"large": "x" * 300000})
        self.assertFalse(self.journal.load("task")["attempts"][0]["axes"]["persisted"])
        self.journal.checkpoint("task", {"complete": True, "terminal": "DONE"})
        record = self.journal.load("task")
        self.assertTrue(record["attempts"][0]["axes"]["persisted"])
        self.assertEqual(record["request"]["checkpoint_attempts"], 1)

    def test_unreadable_observation_is_not_silently_normalized_on_load(self):
        self.start()
        record = self.journal.load("task")
        record["attempts"][0]["evidence"] = {"large": "x" * 20000}
        path = self.journal.path("task")
        path.write_text(json.dumps(record), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ReconciliationRequired):
            self.journal.load("task")
        self.assertEqual(path.read_bytes(), before)


class LocalRunner:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = 0
        self.last_events = {}

    def run_capture(self, *args, **kwargs):
        self.calls += 1
        self.last_events = {"process_start": time.monotonic()}
        if self.mode == "bad_cleanup":
            self.last_events["cleanup_incomplete"] = time.monotonic()
            raise RuntimeError("cleanup unverified")
        if self.mode in ("partial", "recovered"):
            self.last_events["cleanup_end"] = time.monotonic()
            output = '{"type":"result","result":"OK"}' if self.mode == "recovered" else "partial output"
            raise subprocess.TimeoutExpired("fixture", 1, output=output)
        self.last_events["child_exit"] = time.monotonic()
        return (1, '{"type":"error","message":"quota"}', "") if self.mode == "error" else (
            0, '{"type":"result","result":"OK"}', "")


class RecoveryObservationTests(unittest.TestCase):
    def test_configured_capability_is_conservative_and_never_changes_policy(self):
        runner = LocalRunner()
        for sandbox, network, expected in (("read-only", False, "read_only"), ("workspace-write", False, "workspace_write"),
                                           ("workspace-write", True, "workspace_write_network"), ("danger-full-access", True, "unknown")):
            adapter = CodexChatAdapter(runner, cwd=".", model="fixture", sandbox=sandbox, network_access=network)
            self.assertEqual(adapter.effect_capability, expected)
            self.assertEqual((adapter.sandbox, adapter.network_access), (sandbox, network))
        for tools, mode, expected in (("Read Glob Grep", "dontAsk", "read_only"), ("Read Write Edit", "dontAsk", "workspace_write"),
                                      ("Bash Read", "dontAsk", "workspace_write_network"), ("Read", "acceptEdits", "unknown"),
                                      ("Bash(custom pattern)", "dontAsk", "unknown")):
            adapter = ClaudeChatAdapter(runner, cwd=".", permission_mode=mode, allowed_tools=tools)
            self.assertEqual(adapter.effect_capability, expected)

    def test_completion_timeout_cleanup_and_not_started_have_distinct_axes(self):
        for mode, process, result, settled in (("ok", "exited", "final", True), ("partial", "exited", "partial", False),
            ("recovered", "exited", "final", False), ("bad_cleanup", "unverified", "error", False),
            ("error", "exited", "error", True), ("not_started", "not_started", "none", True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS):
                runner = LocalRunner(mode)
                adapter = ClaudeChatAdapter(runner, cwd=tmp, executable_path="fixture", show_progress=False)
                with patch.object(adapter, "_resolve_binary", side_effect=ChatAdapterError("not found")) if mode == "not_started" else patch.object(adapter, "_resolve_binary", return_value="fixture"):
                    if mode in ("ok", "recovered"):
                        self.assertEqual(adapter.send("task", "hello"), "OK")
                    else:
                        with self.assertRaises((RuntimeError, ChatAdapterError)):
                            adapter.send("task", "hello")
                record = ExecutionJournal(Path(tmp) / "workspace/runtime/live_chat").load("live-chat")
                item = record["attempts"][0]
                self.assertEqual((item["axes"]["process"], item["axes"]["result"], item["settled"]), (process, result, settled))
                self.assertFalse(item["axes"]["persisted"])
                self.assertEqual(record["status"], "FINISHED" if settled else "NEEDS_RECONCILIATION")

    def test_git_observer_failure_preserves_reply_and_off_path_does_not_query_git(self):
        for enabled in ("0", "1"):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS | {"SOPHIA_G2_CHAT_RECOVERY": enabled}):
                adapter = ClaudeChatAdapter(LocalRunner(), cwd=tmp, executable_path="fixture", show_progress=False)
                with patch.object(effects.subprocess, "run", side_effect=FileNotFoundError("no git")) as query:
                    self.assertEqual(adapter.send("task", "hi"), "OK")
                    self.assertEqual(query.call_count, 2 if enabled == "1" else 0)
                self.assertEqual(adapter.last_timing.outcome, "completed")
                if enabled == "1":
                    item = ExecutionJournal(Path(tmp) / "workspace/runtime/live_chat").load("live-chat")["attempts"][0]
                    self.assertEqual(item["axes"]["effects"], "unknown")

    def test_message_checkpoint_covers_result_evidence_and_cached_result_makes_no_probe(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FLAGS | {"SOPHIA_G2_MESSAGE_RECOVERY": "1"}):
            agents = {name: cls(LocalRunner(), cwd=tmp, executable_path="fixture", show_progress=False)
                      for name, cls in (("Claude", ClaudeChatAdapter), ("Codex", CodexChatAdapter))}
            session = ChatSession(agents, attachment_root=Path(tmp), printer=lambda *args: None)
            session.handle_line("/request saved @both hello")
            record = ExecutionJournal(Path(tmp) / "workspace/runtime/live_chat").load("live-chat")
            self.assertTrue(all(item["axes"]["persisted"] for item in record["attempts"]))
            with patch.object(effects.subprocess, "run", side_effect=AssertionError("cached result must not probe")) as query:
                session.handle_line("/request saved @both hello")
                query.assert_not_called()


class StatusCLITests(unittest.TestCase):
    def run_status(self, root, task="task"):
        return subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/agents.py"),
            "--runtime-root", str(root), "--execution-status", task], capture_output=True, timeout=10)

    def test_read_only_status_supports_legacy_and_new_records(self):
        for observed in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                journal = ExecutionJournal(root)
                journal.open("agent_supervisor", "task", "attempt", 60)
                journal.attempt_started("task", "attempt", "fixture")
                kwargs = {"axes": axes(), "evidence": {"scope": effects.SCOPE}} if observed else {}
                journal.attempt_finished("task", "attempt", settled=True, detail="done", **kwargs)
                journal.close("task", "DONE")
                before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                run = self.run_status(root)
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(run.stdout)
                self.assertEqual(result["attempts"][0]["axes"], axes() if observed else None)
                self.assertEqual(before, {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_unknown_and_unreadable_status_create_or_release_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing"
            self.assertEqual(self.run_status(root).returncode, 1)
            self.assertFalse(root.exists())
            (root / "task").mkdir(parents=True)
            path = root / "task/execution.json"
            path.write_text("broken")
            self.assertEqual(self.run_status(root).returncode, 1)
            self.assertEqual(path.read_text(), "broken")
