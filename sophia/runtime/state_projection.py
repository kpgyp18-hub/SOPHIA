"""Read-only projection of execution and task records onto one canonical status.

Stage S1 of output/STATE_MAPPING_DESIGN_20261118.md. Nothing here writes,
repairs or changes admission: the journal keeps deciding what may run again.

Two axes are kept apart on purpose:
  lifecycle  -- where the request is semantically (TaskStatus / tracker)
  execution  -- whether execution ownership and side effects are settled (journal)
A timed-out or cancelled attempt that actually ran is therefore
execution=NEEDS_RECONCILIATION, not lifecycle=TIMED_OUT/CANCELLED.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

SCHEMA_VERSION = 1

# Lifecycle axis (A) and execution axis (B).
QUEUED, RUNNING, WAITING_APPROVAL = "QUEUED", "RUNNING", "WAITING_APPROVAL"
SUCCEEDED, FAILED, CANCELLED, TIMED_OUT = "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"
IDLE, CLAIMED, SETTLED, NEEDS_RECONCILIATION = "IDLE", "CLAIMED", "SETTLED", "NEEDS_RECONCILIATION"
TERMINAL_LIFECYCLE = frozenset({SUCCEEDED, FAILED, CANCELLED, TIMED_OUT})

_TERMINAL_TO_LIFECYCLE = {"DONE": SUCCEEDED, "FAILED": FAILED, "EXPIRED": TIMED_OUT, "RECONCILED": FAILED}

# TaskStatus value -> (lifecycle, note). ESCALATED and PAUSED use the
# conservative mapping decided on 2026-11-18 until Core defines them.
_TASK_STATUS = {
    "received": (QUEUED, None), "classified": (QUEUED, None), "routed": (QUEUED, None), "planned": (QUEUED, None),
    "executing": (RUNNING, None), "verifying": (RUNNING, None), "reviewing": (RUNNING, None),
    "diagnosing": (RUNNING, None), "retry": (RUNNING, None), "rollback": (RUNNING, None),
    "cancel_requested": (RUNNING, "cancel_pending"), "cancelling": (RUNNING, "cancel_pending"),
    "completed": (SUCCEEDED, None),
    "failed": (FAILED, None), "recursion_blocked": (FAILED, None), "max_depth_reached": (FAILED, None),
    "cancel_failed": (FAILED, None),
    "cancelled": (CANCELLED, None),
    "user_decision_required": (WAITING_APPROVAL, None), "safety_hold": (WAITING_APPROVAL, None),
    "paused": (WAITING_APPROVAL, "paused_conservative_mapping"),
    "escalated": (SUCCEEDED, "escalated_conservative_mapping"),
}


def project_execution(record: Mapping[str, Any] | None,
                      owner_alive: Callable[[int], bool] | None = None) -> dict[str, Any]:
    """Execution axis from one journal record (None = no record)."""
    if record is None:
        return {"execution": IDLE, "lifecycle_hint": None}
    status, terminal = record.get("status"), record.get("terminal")
    attempts = record.get("attempts") or []
    if status == "RUNNING":
        alive = None
        if owner_alive is not None:
            try:
                alive = bool(owner_alive(record.get("owner_pid")))
            except Exception:  # an unknown probe is not proof of death
                alive = None
        if alive is False:
            if attempts:
                return {"execution": NEEDS_RECONCILIATION, "lifecycle_hint": None,
                        "reason": "owner exited after starting an attempt"}
            return {"execution": CLAIMED, "lifecycle_hint": QUEUED, "reason": "owner exited before any attempt"}
        return {"execution": RUNNING, "lifecycle_hint": RUNNING,
                "owner_alive": "yes" if alive else "unknown"}
    if status == "NEEDS_RECONCILIATION":
        return {"execution": NEEDS_RECONCILIATION, "lifecycle_hint": None, "reason": record.get("reason")}
    if status == "FINISHED":
        if terminal == "ACKNOWLEDGED_HISTORY":
            return {"execution": SETTLED, "lifecycle_hint": None, "history_marker": True}
        result = {"execution": SETTLED, "lifecycle_hint": _TERMINAL_TO_LIFECYCLE.get(terminal, FAILED)}
        if terminal == "RECONCILED":
            result["disposition"] = "operator_reconciled"
        elif terminal not in _TERMINAL_TO_LIFECYCLE:
            result["unmapped_terminal"] = str(terminal)[:64]
        return result
    return {"execution": NEEDS_RECONCILIATION, "lifecycle_hint": None, "reason": "unknown record status"}


def project_task(task_status: str | None) -> dict[str, Any]:
    """Lifecycle axis from a TaskStatus value (tracker or Core)."""
    if task_status is None:
        return {"lifecycle": None}
    lifecycle, note = _TASK_STATUS.get(str(task_status).lower(), (None, "unknown_task_status"))
    result = {"lifecycle": lifecycle, "phase": str(task_status).lower()}
    if note:
        result["note"] = note
    return result


def compose(execution: str, lifecycle: str | None) -> tuple[str, bool]:
    """Display status by the precedence in the design document. Returns (status, consistent)."""
    if execution == NEEDS_RECONCILIATION:
        return NEEDS_RECONCILIATION, True
    if lifecycle == WAITING_APPROVAL:
        return WAITING_APPROVAL, True
    if execution == CLAIMED:
        return CLAIMED, True
    if execution == RUNNING:
        return RUNNING, lifecycle in (None, QUEUED, RUNNING)
    if lifecycle == RUNNING:
        return RUNNING, execution in (IDLE,)
    if execution == IDLE:
        return (lifecycle or QUEUED), lifecycle in (None, QUEUED) or lifecycle in TERMINAL_LIFECYCLE
    if execution == SETTLED and lifecycle in TERMINAL_LIFECYCLE:
        return lifecycle, True
    # Settled execution without a terminal lifecycle: treat conservatively.
    return NEEDS_RECONCILIATION, False


def project_status(record: Mapping[str, Any] | None, task_status: str | None = None, *,
                   owner_alive: Callable[[int], bool] | None = None) -> dict[str, Any]:
    """Canonical view. Without a task record the journal's lifecycle hint stands in."""
    execution = project_execution(record, owner_alive)
    task = project_task(task_status)
    lifecycle = task["lifecycle"] if task["lifecycle"] is not None else execution.get("lifecycle_hint")
    display, consistent = compose(execution["execution"], lifecycle)
    if not consistent:
        display = NEEDS_RECONCILIATION
    view = {"schema_version": SCHEMA_VERSION, "status": display, "execution": execution["execution"],
            "lifecycle": lifecycle, "lifecycle_source": "task" if task["lifecycle"] is not None else "journal",
            "consistent": consistent}
    for key in ("reason", "disposition", "unmapped_terminal", "history_marker", "owner_alive"):
        if key in execution:
            view[key] = execution[key]
    for key in ("phase", "note"):
        if key in task:
            view[f"task_{key}"] = task[key]
    return view


def project_long_task(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Execution axis for scripts/supervisor/long_task.py state files."""
    if state is None:
        return {"execution": IDLE, "lifecycle_hint": None}
    status = state.get("status")
    if status == "RUNNING":
        return {"execution": RUNNING, "lifecycle_hint": RUNNING}
    if status == "FINISHED" and state.get("cleanup") in ("child_gone", "operator_verified"):
        return {"execution": SETTLED, "lifecycle_hint": SUCCEEDED if state.get("exit_code") == 0 else FAILED}
    return {"execution": NEEDS_RECONCILIATION, "lifecycle_hint": None,
            "reason": "lost" if status == "LOST" else "cleanup unverified"}
