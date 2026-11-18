"""State mapping S1: read-only canonical projection and fixed journal terminals."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sophia.runtime import state_projection as sp
from sophia.runtime.execution_recovery import ExecutionJournal, settle_attempt

ROOT = Path(__file__).resolve().parents[1]


def record(status, terminal=None, attempts=(), **extra):
    data = {"status": status, "attempts": list(attempts), "owner_pid": 4242, **extra}
    if terminal is not None:
        data["terminal"] = terminal
    return data


@pytest.mark.parametrize("terminal, lifecycle", [
    ("DONE", "SUCCEEDED"), ("FAILED", "FAILED"), ("EXPIRED", "TIMED_OUT"), ("RECONCILED", "FAILED")])
def test_finished_terminals_map_to_settled_lifecycle(terminal, lifecycle):
    view = sp.project_status(record("FINISHED", terminal))
    assert (view["status"], view["execution"], view["lifecycle"]) == (lifecycle, "SETTLED", lifecycle)
    assert view["consistent"] is True
    if terminal == "RECONCILED":
        assert view["disposition"] == "operator_reconciled"  # never upgraded to success


def test_unknown_legacy_terminal_is_failed_not_success():
    view = sp.project_status(record("FINISHED", "something-else"))
    assert view["status"] == "FAILED" and view["unmapped_terminal"] == "something-else"


def test_needs_reconciliation_overrides_every_lifecycle():
    for task_status in (None, "completed", "executing", "user_decision_required"):
        view = sp.project_status(record("NEEDS_RECONCILIATION", "NEED_USER", reason="timeout"), task_status)
        assert view["status"] == "NEEDS_RECONCILIATION"


def test_running_owner_states():
    alive = sp.project_status(record("RUNNING", attempts=[{}]), owner_alive=lambda pid: True)
    assert alive["status"] == "RUNNING" and alive["owner_alive"] == "yes"
    unknown = sp.project_status(record("RUNNING", attempts=[{}]))
    assert unknown["status"] == "RUNNING" and unknown["owner_alive"] == "unknown"
    claimed = sp.project_status(record("RUNNING"), owner_alive=lambda pid: False)
    assert claimed["status"] == "CLAIMED"
    lost = sp.project_status(record("RUNNING", attempts=[{}]), owner_alive=lambda pid: False)
    assert lost["status"] == "NEEDS_RECONCILIATION"
    broken_probe = sp.project_status(record("RUNNING", attempts=[{}]), owner_alive=lambda pid: 1 / 0)
    assert broken_probe["status"] == "RUNNING" and broken_probe["owner_alive"] == "unknown"


@pytest.mark.parametrize("task_status, lifecycle", [
    ("received", "QUEUED"), ("planned", "QUEUED"), ("executing", "RUNNING"), ("retry", "RUNNING"),
    ("cancel_requested", "RUNNING"), ("completed", "SUCCEEDED"), ("max_depth_reached", "FAILED"),
    ("cancelled", "CANCELLED"), ("safety_hold", "WAITING_APPROVAL"), ("paused", "WAITING_APPROVAL"),
    ("escalated", "SUCCEEDED")])
def test_task_status_lifecycle_mapping(task_status, lifecycle):
    assert sp.project_task(task_status)["lifecycle"] == lifecycle


def test_conservative_mappings_are_labelled():
    assert sp.project_task("paused")["note"] == "paused_conservative_mapping"
    assert sp.project_task("escalated")["note"] == "escalated_conservative_mapping"
    assert sp.project_task("nonsense")["lifecycle"] is None


def test_every_task_status_value_is_mapped():
    from sophia.runtime.task import TaskStatus
    assert {status.value for status in TaskStatus} == set(sp._TASK_STATUS)


def test_composition_precedence_and_inconsistency():
    # tracker only (recovery flags off): no journal record is normal
    assert sp.project_status(None, "completed")["status"] == "SUCCEEDED"
    assert sp.project_status(None, "executing")["status"] == "RUNNING"
    assert sp.project_status(None, "user_decision_required")["status"] == "WAITING_APPROVAL"
    # task says done while execution still runs -> inconsistent, shown conservatively
    view = sp.project_status(record("RUNNING", attempts=[{}]), "completed", owner_alive=lambda pid: True)
    assert (view["status"], view["consistent"]) == ("NEEDS_RECONCILIATION", False)
    # settled execution with a task still queued -> inconsistent
    view = sp.project_status(record("FINISHED", "DONE"), "received")
    assert (view["status"], view["consistent"]) == ("NEEDS_RECONCILIATION", False)
    assert sp.project_status(record("FINISHED", "DONE"), "completed")["status"] == "SUCCEEDED"


def test_timeout_and_cancel_that_ran_are_reconciliation_not_lifecycle_terminal():
    # Matches the 2026-11-18 live-provider results S1/S6/S8.
    for outcome, events in (("timeout", {"process_start": 1.0, "cleanup_end": 2.0}),
                            ("cancelled", {"process_start": 1.0, "child_exit": 2.0})):
        settled, _ = settle_attempt(outcome, events, None)
        assert settled is False


def test_long_task_projection():
    assert sp.project_long_task({"status": "RUNNING"})["execution"] == "RUNNING"
    done = sp.project_long_task({"status": "FINISHED", "cleanup": "child_gone", "exit_code": 0})
    assert (done["execution"], done["lifecycle_hint"]) == ("SETTLED", "SUCCEEDED")
    assert sp.project_long_task({"status": "FINISHED", "cleanup": "child_gone", "exit_code": 2})["lifecycle_hint"] == "FAILED"
    assert sp.project_long_task({"status": "FINISHED", "cleanup": "unknown"})["execution"] == "NEEDS_RECONCILIATION"
    assert sp.project_long_task({"status": "LOST"})["execution"] == "NEEDS_RECONCILIATION"


def test_close_normalizes_unknown_terminal_and_keeps_original(tmp_path):
    owner = ExecutionJournal(tmp_path, owner_alive=lambda pid: False)
    owner.open("live_chat", "task", "a1", 60)
    assert owner.close("task", "SOMETHING-ODD" + "x" * 100) == "FINISHED"
    saved = owner.load("task")
    assert saved["terminal"] == "FAILED" and saved["terminal_original"] == ("SOMETHING-ODD" + "x" * 100)[:64]
    owner.open("live_chat", "task", "a2", 60)
    owner.close("task", "NEED_USER")
    saved = owner.load("task")
    assert saved["terminal"] == "NEED_USER" and "terminal_original" not in saved


def test_cli_status_adds_canonical_view_without_writing(tmp_path):
    owner = ExecutionJournal(tmp_path, owner_alive=lambda pid: False)
    owner.open("live_chat", "task", "a1", 60, request_id="r1",
               payload_hash=hashlib.sha256(b"r1").hexdigest(), checkpoint={"complete": True, "terminal": "DONE"})
    owner.close("task", "DONE")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    out = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ROOT / "scripts/agents.py"), "--execution-status",
                          "task", "--runtime-root", str(tmp_path)], capture_output=True, text=True,
                         encoding="utf-8", cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    canonical = json.loads(out.stdout)["canonical"]
    assert canonical["status"] == "SUCCEEDED" and canonical["execution"] == "SETTLED"
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
