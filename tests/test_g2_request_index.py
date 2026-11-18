"""C: request archive cache, explicit corrupt-history acknowledgement and R8."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from sophia.runtime.execution_recovery import ExecutionJournal, ReconciliationRequired, OwnedElsewhere

ROOT = Path(__file__).resolve().parents[1]


def journal(root, alive=False):
    return ExecutionJournal(root, owner_alive=lambda pid: alive)


def finish(owner, identity):
    owner.open("live_chat", "task", f"a-{identity}", 60, request_id=identity,
               payload_hash=hashlib.sha256(identity.encode()).hexdigest(), checkpoint={"complete": True, "terminal": "DONE"})
    owner.close("task", "DONE")


def test_valid_index_reads_only_the_matched_archive_and_appends_new_generations(tmp_path):
    owner = journal(tmp_path)
    for index in range(30):
        finish(owner, f"r{index:02}")
    with patch.object(owner, "_read_archive", wraps=owner._read_archive) as reader:
        assert owner.request_record("task", "r00")["request"]["id"] == "r00"
        assert reader.call_count == 1
    data = (tmp_path / "task/requests.index.jsonl").read_bytes()
    with patch.object(owner, "_read_archive", wraps=owner._read_archive) as reader:
        finish(owner, "new")
        assert reader.call_count == 0
    updated = (tmp_path / "task/requests.index.jsonl").read_bytes()
    assert updated.startswith(data) and len(updated.splitlines()) == 30


@pytest.mark.parametrize("damage", ["missing", "broken", "identity", "unterminated"])
def test_invalid_index_rebuilds_from_all_archives_without_losing_identity(tmp_path, damage):
    owner = journal(tmp_path)
    for identity in ("first", "second", "third"):
        finish(owner, identity)
    path = tmp_path / "task/requests.index.jsonl"
    if damage == "missing":
        path.unlink()
    elif damage == "broken":
        path.write_text("{broken")
    elif damage == "identity":
        path.write_bytes(path.read_bytes().replace(b'"first"', b'"other"'))
    else:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with patch.object(owner, "_read_archive", wraps=owner._read_archive) as reader:
        assert owner.request_record("task", "first")["request"]["id"] == "first"
        assert reader.call_count >= 2
    assert len([json.loads(line) for line in path.read_bytes().splitlines()]) == 2


def test_long_terminal_label_keeps_the_index_usable(tmp_path):
    # N6: an unbounded terminal label must not make every lookup a full scan.
    # close() now normalizes unknown labels, so simulate records written before that.
    import sophia.runtime.execution_recovery as recovery
    owner = journal(tmp_path)
    legacy = "X" * 10000
    for identity in ("first", "second"):
        owner.open("live_chat", "task", f"a-{identity}", 60, request_id=identity,
                   payload_hash=hashlib.sha256(identity.encode()).hexdigest(), checkpoint={"complete": True})
        with patch.object(recovery, "OWNER_TERMINALS", recovery.OWNER_TERMINALS | {legacy}):
            owner.close("task", legacy)
    finish(owner, "third")
    rows = [json.loads(line) for line in (tmp_path / "task/requests.index.jsonl").read_bytes().splitlines()]
    assert [len(row["terminal"]) for row in rows] == [64, 64]
    assert all(len(json.dumps(row)) < 4096 for row in rows)
    fresh = journal(tmp_path)
    with patch.object(fresh, "_read_archive", wraps=fresh._read_archive) as reader:
        assert fresh.request_record("task", "absent") is None
        assert reader.call_count == 0  # served from the index, no rebuild
        assert fresh.request_record("task", "first")["request"]["id"] == "first"
        assert reader.call_count == 1


def test_requestless_history_costs_one_rebuild_then_request_path_is_indexed(tmp_path):
    # N5: request-less rotations do not maintain the index; the first request
    # open rebuilds once, after which R8 keeps the task on the indexed path.
    owner = journal(tmp_path)
    for index in range(3):
        owner.open("live_chat", "task", f"plain-{index}", 60)
        owner.close("task", "DONE")
    assert not (tmp_path / "task/requests.index.jsonl").exists()
    with patch.object(owner, "_read_archive", wraps=owner._read_archive) as reader:
        finish(owner, "first")
        assert reader.call_count == 2  # one verified scan of the existing archives
    with patch.object(owner, "_read_archive", wraps=owner._read_archive) as reader:
        finish(owner, "second")
        assert reader.call_count == 0
    with pytest.raises(ReconciliationRequired):
        owner.open("live_chat", "task", "plain-again", 60)


def test_archive_count_and_metadata_changes_trigger_rebuild_or_fail_closed(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    finish(owner, "current")
    path = tmp_path / "task/execution.g1.json"
    extra = json.loads(path.read_text())
    extra["generation"] = 999
    extra["request"]["id"] = "extra"
    (tmp_path / "task/execution.g999.json").write_text(json.dumps(extra))
    with patch.object(owner, "_read_archive", wraps=owner._read_archive) as reader:
        assert owner.request_record("task", "first")["request"]["id"] == "first"
        assert reader.call_count >= 2
    path.write_text("corrupt")
    with pytest.raises(ReconciliationRequired, match="archived request history"):
        finish(owner, "must-not-start")
    assert owner.load("task")["request"]["id"] == "current"


def test_cache_write_failure_does_not_replace_verified_archive_evidence(tmp_path):
    owner = journal(tmp_path)
    with patch.object(owner, "_store_index", return_value=None):
        finish(owner, "first")
        finish(owner, "second")
    assert owner.request_record("task", "first")["request"]["id"] == "first"


def test_restart_after_archive_before_fresh_record_never_reuses_generation(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    finish(owner, "second")
    owner.path("task").rename(tmp_path / "task/execution.g2.json")
    saved = {path.name: path.read_bytes() for path in (tmp_path / "task").glob("execution.g*.json")}
    finish(journal(tmp_path), "third")
    assert journal(tmp_path).load("task")["generation"] == 3
    assert all((tmp_path / "task" / name).read_bytes() == data for name, data in saved.items())


@pytest.mark.parametrize("closed", [True, False])
def test_requestless_open_cannot_take_over_request_bound_record(tmp_path, closed):
    owner = journal(tmp_path)
    owner.open("live_chat", "task", "first", 60, request_id="bound", payload_hash="0" * 64, checkpoint={})
    if closed:
        owner.close("task", "DONE")
    before = owner.path("task").read_bytes()
    with pytest.raises(ReconciliationRequired, match="request-bound"):
        journal(tmp_path).open("live_chat", "task", "plain", 60)
    assert owner.path("task").read_bytes() == before


def test_acknowledgement_preserves_bytes_note_and_gap_across_generations(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    raw = tmp_path / "task/execution.unreadable-1.json"
    raw.write_bytes(b"broken evidence\xff")
    with pytest.raises(ReconciliationRequired, match="unreadable"):
        finish(owner, "blocked")
    record = owner.acknowledge_unreadable("task", "Inspected original corruption; prior IDs are unknown")
    ack = record["history_acknowledgements"][-1]
    assert ack["note"] and ack["by_pid"] and ack["at_wall"]
    assert not raw.exists() and (raw.parent / ack["file"]).read_bytes() == b"broken evidence\xff"
    for identity in ("second", "third"):
        finish(owner, identity)
        current = owner.load("task")
        assert current["request_history_incomplete"]
        assert owner.history_status("task", current)["limitation"]
        assert owner.history_status("task", current)["unacknowledged_files"] == 0


def test_acknowledgement_does_not_settle_a_blocked_attempt(tmp_path):
    owner = journal(tmp_path)
    owner.open("live_chat", "task", "first", 60, request_id="bound", payload_hash="0" * 64, checkpoint={})
    owner.attempt_started("task", "first", "fixture")
    owner.close("task", "FAILED")
    (tmp_path / "task/execution.unreadable-1.json").write_text("old evidence")
    record = owner.acknowledge_unreadable("task", "Acknowledged history only")
    assert record["status"] == "NEEDS_RECONCILIATION" and not record["attempts"][0]["settled"]
    with pytest.raises(ReconciliationRequired):
        finish(owner, "still-blocked")


def test_live_owner_missing_note_and_unknown_task_cannot_be_acknowledged(tmp_path):
    owner = journal(tmp_path, alive=True)
    with pytest.raises(ValueError):
        owner.acknowledge_unreadable("missing", "")
    with pytest.raises(ValueError):
        owner.acknowledge_unreadable("missing", "a note")
    assert not (tmp_path / "missing").exists()
    owner.open("live_chat", "task", "a", 60)
    (tmp_path / "task/execution.unreadable-1.json").write_text("broken")
    before = owner.path("task").read_bytes()
    with pytest.raises(OwnedElsewhere):
        owner.acknowledge_unreadable("task", "must not touch live owner")
    assert owner.path("task").read_bytes() == before


def test_manual_rename_without_ack_note_is_not_a_history_release(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    evidence = tmp_path / "task/execution.unreadable-manual.acknowledged.json"
    evidence.write_text("preserved")
    with pytest.raises(ReconciliationRequired):
        finish(owner, "blocked")
    owner.acknowledge_unreadable("task", "Explicitly inspected the manually renamed evidence")
    finish(owner, "allowed")
    assert evidence.read_text() == "preserved"


def test_partial_acknowledgement_rename_failure_keeps_unmoved_evidence_blocking(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    raw = tmp_path / "task/execution.unreadable-1.json"
    raw.write_text("preserved")
    with patch("sophia.runtime.execution_recovery.os.rename", side_effect=OSError("move failed")):
        with pytest.raises(OSError):
            owner.acknowledge_unreadable("task", "record disposition before file move")
    assert raw.read_text() == "preserved"
    with pytest.raises(ReconciliationRequired):
        finish(owner, "blocked")
    owner.acknowledge_unreadable("task", "retry after inspecting partial acknowledgement")
    finish(owner, "allowed")


def test_cli_ack_and_read_only_archive_status(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    finish(owner, "second")
    raw = tmp_path / "task/execution.unreadable-1.json"
    raw.write_text("evidence")
    command = [sys.executable, "-X", "utf8", str(ROOT / "scripts/agents.py"), "--runtime-root", str(tmp_path)]
    refused = subprocess.run(command + ["--acknowledge-unreadable", "task"], capture_output=True, timeout=10)
    assert refused.returncode == 2 and raw.exists()
    run = subprocess.run(command + ["--acknowledge-unreadable", "task", "--note", "Inspected fixture history"],
                         capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert b"uniqueness is unverified" in run.stdout
    before = {path.name: path.read_bytes() for path in (tmp_path / "task").iterdir() if path.is_file()}
    status = subprocess.run(command + ["--execution-status", "task"], capture_output=True, timeout=10)
    result = json.loads(status.stdout)
    assert result["history"]["archive_files"] == 1
    assert result["history"]["archive_bytes"] == (tmp_path / "task/execution.g1.json").stat().st_size
    assert result["history"]["request_history_incomplete"]
    assert before == {path.name: path.read_bytes() for path in (tmp_path / "task").iterdir() if path.is_file()}


def test_unreadable_current_record_requires_reconcile_before_history_ack(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    owner.path("task").write_bytes(b"corrupt current record")
    with pytest.raises(ReconciliationRequired):
        owner.acknowledge_unreadable("task", "cannot bypass unreadable current state")
    owner.reconcile("task", "inspected missing execution evidence and local effects")
    owner.acknowledge_unreadable("task", "prior request identities are not recoverable")
    assert any(path.read_bytes() == b"corrupt current record" for path in (tmp_path / "task").glob("*.acknowledged.json"))
    finish(owner, "new")
    assert owner.load("task")["request_history_incomplete"]


def test_history_only_directory_can_be_acknowledged_without_losing_evidence(tmp_path):
    directory = tmp_path / "task"
    directory.mkdir()
    raw = directory / "execution.unreadable-orphan.json"
    raw.write_text("orphan evidence")
    owner = journal(tmp_path)
    owner.acknowledge_unreadable("task", "verified orphan evidence; no current execution record")
    assert (directory / "execution.unreadable-orphan.acknowledged.json").read_text() == "orphan evidence"
    finish(owner, "new")
    assert owner.load("task")["request_history_incomplete"]


def test_repeated_corruption_recovery_never_collides_with_older_generations(tmp_path):
    owner = journal(tmp_path)
    finish(owner, "first")
    finish(owner, "second")
    for index in range(2):
        originals = {path.name: path.read_bytes() for path in (tmp_path / "task").glob("execution.g*.json")}
        owner.path("task").write_text(f"corruption {index}")
        owner.reconcile("task", "inspected interrupted fixture state")
        owner.acknowledge_unreadable("task", "acknowledge old identity gap")
        finish(owner, f"new{index}")
        assert all((tmp_path / "task" / name).read_bytes() == content for name, content in originals.items())


def test_same_second_reconcile_preserves_each_unreadable_record(tmp_path):
    owner = ExecutionJournal(tmp_path, owner_alive=lambda pid: False, wall_clock=lambda: 1000.0)
    finish(owner, "first")
    for index in range(2):
        owner.path("task").write_text(f"raw evidence {index}")
        owner.reconcile("task", "inspected corruption")
    evidence = list((tmp_path / "task").glob("execution.unreadable-*.json"))
    assert len(evidence) == 2
    assert {path.read_text() for path in evidence} == {"raw evidence 0", "raw evidence 1"}


def test_requestless_generation_after_missing_current_does_not_overwrite_archives(tmp_path):
    owner = journal(tmp_path)
    owner.open("agent_supervisor", "task", "first", 60)
    owner.close("task", "DONE")
    owner.path("task").rename(tmp_path / "task/execution.g1.json")
    before = (tmp_path / "task/execution.g1.json").read_bytes()
    owner.open("agent_supervisor", "task", "second", 60)
    assert owner.load("task")["generation"] == 2
    assert (tmp_path / "task/execution.g1.json").read_bytes() == before
