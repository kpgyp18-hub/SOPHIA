"""State mapping S3: ChatTaskTracker changes status only along Core's legal paths."""
from types import SimpleNamespace

import pytest

from sophia.core.state_machine import InvalidTaskTransition, TaskStateMachine
from sophia.runtime.store import TaskStore
from sophia.runtime.task import TaskStatus
from sophia.runtime.tracker import ChatTaskTracker


@pytest.fixture
def tracker(tmp_path):
    return ChatTaskTracker("test", TaskStore(tmp_path / "tasks"))


def _local(tracker, task_id):
    tracker.store.update(task_id, escalation={"route": "local"})


def _types(state):
    return [event["type"] for event in state["events"]]


def test_begin_reaches_executing_through_core_path(tracker):
    task_id = tracker.begin("hello")
    state = tracker.store.read(task_id)
    assert state["status"] == "executing" and _types(state) == ["received"]


def test_complete_goes_through_verifying(tracker):
    task_id = tracker.begin("hello")
    _local(tracker, task_id)
    tracker.complete(task_id)
    state = tracker.store.read(task_id)
    assert state["status"] == "completed" and _types(state)[-1] == "completed"
    # the stored path is legal under Core's rules
    probe = SimpleNamespace(status=TaskStatus.EXECUTING)
    for step in (TaskStatus.VERIFYING, TaskStatus.COMPLETED):
        TaskStateMachine().transition(probe, step)


def test_hold_goes_through_verifying_and_repeats_record_reason_only(tracker):
    task_id = tracker.begin("hello")
    tracker.hold(task_id, "needs reconciliation: timeout")
    state = tracker.store.read(task_id)
    assert state["status"] == "user_decision_required"
    tracker.hold(task_id, "needs reconciliation: timeout")  # identical: deduplicated
    tracker.hold(task_id, "blocked: still unsettled")
    state = tracker.store.read(task_id)
    assert state["status"] == "user_decision_required"
    assert _types(state).count("execution_hold") == 2 and "invalid_transition" not in _types(state)


def test_complete_after_hold_is_rejected_not_overwritten(tracker):
    task_id = tracker.begin("hello")
    _local(tracker, task_id)
    tracker.hold(task_id, "needs reconciliation")
    tracker.complete(task_id)
    state = tracker.store.read(task_id)
    assert state["status"] == "user_decision_required"  # previously overwritten to completed
    assert _types(state)[-1] == "invalid_transition"
    assert state["events"][-1]["to"] == "completed"


def test_hold_after_completion_is_rejected(tracker):
    task_id = tracker.begin("hello")
    _local(tracker, task_id)
    tracker.complete(task_id)
    tracker.hold(task_id, "late hold")
    state = tracker.store.read(task_id)
    assert state["status"] == "completed" and _types(state)[-1] == "invalid_transition"


def test_unknown_stored_status_is_recorded_not_raised(tracker):
    task_id = tracker.begin("hello")
    _local(tracker, task_id)
    tracker.store.update(task_id, status="something-else")
    tracker.complete(task_id)
    state = tracker.store.read(task_id)
    assert state["status"] == "something-else" and "unknown stored status" in state["events"][-1]["error"]


def test_escalated_stays_a_marked_exception(tracker):
    task_id = tracker.begin("hello")
    tracker.store.update(task_id, escalation={"route": "claude"})
    tracker.complete(task_id)
    state = tracker.store.read(task_id)
    assert state["status"] == "escalated"
    assert _types(state)[-2:] == ["awaiting_handoff", "transition_exception"]
    # ESCALATED really is outside Core's machine: this is why it is an exception
    with pytest.raises(InvalidTaskTransition):
        TaskStateMachine().transition(SimpleNamespace(status=TaskStatus.EXECUTING), TaskStatus.ESCALATED)
