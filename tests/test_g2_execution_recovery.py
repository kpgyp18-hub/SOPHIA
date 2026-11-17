"""G2 C restart recovery contract: no extended deadline, no blind re-execution."""
from __future__ import annotations

import json
import subprocess

import pytest

from sophia.runtime.execution_recovery import (FINISHED, NEEDS_RECONCILIATION, RUNNING,
                                               ExecutionJournal, OwnedElsewhere,
                                               ReconciliationRequired, StaleOwner,
                                               settle_attempt)


class Clock:
    """A wall clock and a monotonic clock that advance together unless told otherwise."""

    def __init__(self, wall: float = 1_000_000.0, mono: float = 50.0) -> None:
        self.wall_now, self.mono_now = wall, mono

    def advance(self, seconds: float) -> None:
        self.wall_now += seconds
        self.mono_now += seconds

    def wall(self) -> float:
        return self.wall_now

    def mono(self) -> float:
        return self.mono_now


def journal(root, clock: Clock, *, owner_alive: bool = False) -> ExecutionJournal:
    """A new owner, as a restarted process would be."""
    return ExecutionJournal(root, owner_alive=lambda pid: owner_alive,
                            wall_clock=clock.wall, monotonic=clock.mono)


def stored(root, task_id: str = "task-1") -> dict:
    return json.loads((root / task_id / "execution.json").read_text(encoding="utf-8"))


def test_new_task_gets_its_full_budget_and_a_durable_record(tmp_path) -> None:
    clock = Clock()
    contract = journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    assert contract.remaining_s(now=clock.mono()) == 300
    record = stored(tmp_path)
    assert (record["status"], record["generation"], record["timeout_s"]) == (RUNNING, 1, 300)
    assert record["started_wall"] == clock.wall()


def test_restart_continues_the_original_deadline_instead_of_a_new_one(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    # The owner dies before any attempt; the host comes back 120s later with an
    # unrelated monotonic clock (a new process or a reboot).
    clock.wall_now += 120
    clock.mono_now = 7.0
    restored = journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 300)
    assert restored.remaining_s(now=clock.mono()) == pytest.approx(180)
    assert restored.attempt_id == "a2" and restored.task_id == "task-1"
    record = stored(tmp_path)
    assert (record["generation"], record["takeovers"], record["timeout_s"]) == (1, 1, 300)


def test_restart_cannot_ask_for_more_than_what_was_left(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    clock.advance(100)
    bigger = journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 9000)
    assert bigger.remaining_s(now=clock.mono()) == pytest.approx(200)


def test_restart_may_ask_for_less_than_what_was_left(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    clock.advance(10)
    smaller = journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 30)
    assert smaller.remaining_s(now=clock.mono()) == pytest.approx(30)


def test_restart_after_the_deadline_starts_nothing(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    clock.advance(301)
    with pytest.raises(TimeoutError, match="before restart"):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 300)
    record = stored(tmp_path)
    assert (record["status"], record["terminal"]) == (FINISHED, "EXPIRED")


def test_backwards_wall_clock_fails_closed(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    clock.wall_now -= 3600
    with pytest.raises(ReconciliationRequired, match="wall clock moved backwards"):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 300)
    assert stored(tmp_path)["status"] == NEEDS_RECONCILIATION
    # ...and it stays blocked however often the restart is repeated.
    with pytest.raises(ReconciliationRequired):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a3", 300)


def test_record_of_a_live_owner_is_left_untouched(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    before = stored(tmp_path)
    clock.advance(5)
    with pytest.raises(OwnedElsewhere, match="live process"):
        journal(tmp_path, clock, owner_alive=True).open("agent_supervisor", "task-1", "a2", 300)
    assert stored(tmp_path) == before


def test_unknown_liveness_is_treated_as_alive(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    default = ExecutionJournal(tmp_path, wall_clock=clock.wall, monotonic=clock.mono)
    with pytest.raises(OwnedElsewhere):
        default.open("agent_supervisor", "task-1", "a2", 300)


@pytest.mark.parametrize("finish", [None, (True, "completed"), (False, "timeout: interrupted")])
def test_interrupted_task_with_any_started_attempt_is_never_reexecuted(tmp_path, finish) -> None:
    clock = Clock()
    first = journal(tmp_path, clock)
    first.open("agent_supervisor", "task-1", "a1", 300)
    first.attempt_started("task-1", "a1", "01-plan")
    if finish is not None:
        first.attempt_finished("task-1", "a1", settled=finish[0], detail=finish[1])
    clock.advance(1)
    for attempt in ("a2", "a3"):
        with pytest.raises(ReconciliationRequired, match="1 started attempt"):
            journal(tmp_path, clock).open("agent_supervisor", "task-1", attempt, 300)
    record = stored(tmp_path)
    assert record["status"] == NEEDS_RECONCILIATION
    assert [item["attempt_id"] for item in record["attempts"]] == ["a1"]


def test_close_blocks_on_an_unsettled_attempt_and_releases_a_settled_one(tmp_path) -> None:
    clock = Clock()
    owner = journal(tmp_path, clock)
    owner.open("agent_supervisor", "task-1", "a1", 300)
    owner.attempt_started("task-1", "a1", "01-plan")
    owner.attempt_finished("task-1", "a1", settled=True, detail="completed")
    owner.attempt_started("task-1", "a2", "02-exec")
    owner.attempt_finished("task-1", "a2", settled=False, detail="timeout: interrupted")
    assert owner.close("task-1", "FAILED", "timed out") == NEEDS_RECONCILIATION
    record = stored(tmp_path)
    assert (record["terminal"], "02-exec" in record["reason"]) == ("FAILED", True)
    with pytest.raises(ReconciliationRequired, match="02-exec"):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a3", 300)

    other = journal(tmp_path, clock)
    other.open("agent_supervisor", "task-2", "b1", 300)
    other.attempt_started("task-2", "b1", "01-plan")
    other.attempt_finished("task-2", "b1", settled=True, detail="nonzero_exit")
    assert other.close("task-2", "FAILED", "exit 2") == FINISHED


def test_finished_task_restarts_as_a_new_generation_and_keeps_the_old_record(tmp_path) -> None:
    clock = Clock()
    owner = journal(tmp_path, clock)
    owner.open("agent_supervisor", "task-1", "a1", 300)
    owner.close("task-1", "FAILED", "exit 2")
    clock.advance(500)
    again = journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 60)
    assert again.remaining_s(now=clock.mono()) == 60
    assert stored(tmp_path)["generation"] == 2
    archived = json.loads((tmp_path / "task-1" / "execution.g1.json").read_text(encoding="utf-8"))
    assert (archived["generation"], archived["terminal"]) == (1, "FAILED")


def test_reconcile_releases_the_task_and_records_who_decided_what(tmp_path) -> None:
    clock = Clock()
    owner = journal(tmp_path, clock)
    owner.open("agent_supervisor", "task-1", "a1", 300)
    owner.attempt_started("task-1", "a1", "01-plan")
    clock.advance(10)
    operator = journal(tmp_path, clock)
    with pytest.raises(ValueError, match="note is required"):
        operator.reconcile("task-1", "  ")
    record = operator.reconcile("task-1", "no codex process left; git status clean")
    assert (record["status"], record["terminal"]) == (FINISHED, "RECONCILED")
    entry = record["reconciliations"][0]
    assert entry["note"] == "no codex process left; git status clean"
    assert entry["from_status"] == RUNNING
    assert record["attempts"][0]["attempt_id"] == "a1"   # evidence is kept
    with pytest.raises(ValueError, match="already settled"):
        operator.reconcile("task-1", "again")
    fresh = journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 120)
    assert fresh.remaining_s(now=clock.mono()) == 120


def test_reconcile_refuses_while_the_owner_is_alive(tmp_path) -> None:
    clock = Clock()
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    with pytest.raises(ReconciliationRequired, match="still alive"):
        journal(tmp_path, clock, owner_alive=True).reconcile("task-1", "checked")
    assert stored(tmp_path)["status"] == RUNNING


def test_stale_owner_cannot_update_a_record_it_lost(tmp_path) -> None:
    clock = Clock()
    stale = journal(tmp_path, clock)
    stale.open("agent_supervisor", "task-1", "a1", 300)
    clock.advance(1)
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 300)   # takeover
    for update in (lambda: stale.attempt_started("task-1", "a1", "01-plan"),
                   lambda: stale.close("task-1", "DONE")):
        with pytest.raises(StaleOwner):
            update()
    assert stored(tmp_path)["attempts"] == []


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"version": 1}),
                                     json.dumps({"version": 2, "task_id": "task-1"})])
def test_unreadable_record_blocks_and_is_preserved_by_reconcile(tmp_path, content) -> None:
    clock = Clock()
    path = tmp_path / "task-1" / "execution.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ReconciliationRequired, match="unreadable"):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a1", 300)
    assert path.read_text(encoding="utf-8") == content
    journal(tmp_path, clock).reconcile("task-1", "record was truncated; nothing running")
    kept = list(path.parent.glob("execution.unreadable-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == content
    journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 300)


def test_pre_journal_interruption_is_recorded_as_blocked(tmp_path) -> None:
    clock = Clock()
    owner = journal(tmp_path, clock)
    owner.mark_interrupted("agent_supervisor", "task-1", "state was RUNNING")
    with pytest.raises(ReconciliationRequired, match="state was RUNNING"):
        owner.open("agent_supervisor", "task-1", "a1", 300)
    owner.mark_interrupted("agent_supervisor", "task-1", "second call changes nothing")
    assert stored(tmp_path)["reason"] == "state was RUNNING"


def test_invalid_requests_fail_closed(tmp_path) -> None:
    clock = Clock()
    owner = journal(tmp_path, clock)
    for surface, task_id, timeout in (("unknown", "task-1", 5), ("live_chat", "../escape", 5),
                                      ("live_chat", "task-1", 0),
                                      ("live_chat", "task-1", float("inf"))):
        with pytest.raises(ValueError):
            owner.open(surface, task_id, "a1", timeout)
    owner.open("live_chat", "task-1", "a1", 5)
    with pytest.raises(ReconciliationRequired, match="another surface"):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 5)
    assert not (tmp_path.parent / "escape").exists()


def test_leftover_lock_blocks_ordinary_use_but_not_an_operator(tmp_path, monkeypatch) -> None:
    import os

    import sophia.runtime.execution_recovery as recovery

    monkeypatch.setattr(recovery, "_LOCK_WAIT_S", 0.05)
    clock = Clock()
    owner = journal(tmp_path, clock)
    owner.open("agent_supervisor", "task-1", "a1", 300)
    owner.attempt_started("task-1", "a1", "01-plan")
    lock = tmp_path / "task-1" / "execution.lock"
    lock.write_text("", encoding="utf-8")
    with pytest.raises(OwnedElsewhere, match="locked"):
        journal(tmp_path, clock).open("agent_supervisor", "task-1", "a2", 300)
    with pytest.raises(OwnedElsewhere, match="locked"):
        journal(tmp_path, clock).reconcile("task-1", "fresh lock is respected")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    assert journal(tmp_path, clock).reconcile("task-1", "lock was an hour old")["status"] == FINISHED
    assert not lock.exists()


@pytest.mark.parametrize("outcome,events,failure,settled", [
    ("completed", {"process_start": 1, "child_exit": 2}, None, True),
    ("nonzero_exit", {}, RuntimeError("exit 2"), True),
    (None, {}, RuntimeError("codex CLI not found"), True),
    ("timeout", {}, TimeoutError("budget exhausted before new attempt"), True),
    ("timeout", {"timeout": 1}, subprocess.TimeoutExpired("codex", 1), True),
    ("timeout", {"process_start": 1, "timeout": 2, "cleanup_end": 3},
     subprocess.TimeoutExpired("codex", 1), False),
    ("timeout", {}, subprocess.TimeoutExpired("codex", 1), False),
    ("timeout", {"process_start": 1}, TimeoutError("raised by a callback"), False),
    ("error", {"process_start": 1, "cleanup_end": 2}, KeyboardInterrupt(), False),
    ("error", {}, RuntimeError("previous agent process has not exited"), False),
    ("completed", {"cleanup_incomplete": 1}, None, False),
    ("timeout", {"process_start": 1, "cleanup_incomplete": 2}, RuntimeError("unverified"), False),
])
def test_only_a_proven_exit_or_a_proven_non_start_is_settled(outcome, events, failure,
                                                             settled) -> None:
    assert settle_attempt(outcome, events, failure)[0] is settled
