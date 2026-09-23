"""Explicit, validated task lifecycle transitions for SophiaCore."""
from __future__ import annotations

from sophia.runtime.task import Task, TaskStatus


class InvalidTaskTransition(ValueError):
    """Raised when code attempts to skip or revive a terminal task state."""


_TERMINAL = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED,
    TaskStatus.SAFETY_HOLD, TaskStatus.USER_DECISION_REQUIRED,
    TaskStatus.RECURSION_BLOCKED, TaskStatus.MAX_DEPTH_REACHED,
    TaskStatus.CANCELLED, TaskStatus.CANCEL_FAILED,
})

_ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.RECEIVED: frozenset({TaskStatus.CLASSIFIED, TaskStatus.CANCEL_REQUESTED}),
    TaskStatus.CLASSIFIED: frozenset({TaskStatus.ROUTED, TaskStatus.USER_DECISION_REQUIRED, TaskStatus.CANCEL_REQUESTED}),
    TaskStatus.ROUTED: frozenset({TaskStatus.PLANNED, TaskStatus.SAFETY_HOLD, TaskStatus.USER_DECISION_REQUIRED, TaskStatus.CANCEL_REQUESTED}),
    TaskStatus.PLANNED: frozenset({TaskStatus.EXECUTING, TaskStatus.RECURSION_BLOCKED, TaskStatus.MAX_DEPTH_REACHED, TaskStatus.CANCEL_REQUESTED}),
    TaskStatus.EXECUTING: frozenset({TaskStatus.VERIFYING, TaskStatus.REVIEWING, TaskStatus.DIAGNOSING, TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.SAFETY_HOLD, TaskStatus.CANCEL_REQUESTED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.REVIEWING, TaskStatus.COMPLETED, TaskStatus.ROLLBACK, TaskStatus.FAILED, TaskStatus.USER_DECISION_REQUIRED}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.COMPLETED, TaskStatus.DIAGNOSING, TaskStatus.FAILED, TaskStatus.PAUSED}),
    TaskStatus.DIAGNOSING: frozenset({TaskStatus.RETRY, TaskStatus.FAILED, TaskStatus.USER_DECISION_REQUIRED}),
    TaskStatus.RETRY: frozenset({TaskStatus.EXECUTING, TaskStatus.PAUSED}),
    TaskStatus.ROLLBACK: frozenset({TaskStatus.FAILED, TaskStatus.DIAGNOSING}),
    TaskStatus.CANCEL_REQUESTED: frozenset({TaskStatus.CANCELLING}),
    TaskStatus.CANCELLING: frozenset({TaskStatus.CANCELLED, TaskStatus.CANCEL_FAILED}),
}


class TaskStateMachine:
    """Owns legal lifecycle transitions; it does not perform any task work."""

    def transition(self, task: Task, target: TaskStatus) -> TaskStatus:
        if task.status is target:
            return target
        if task.status in _TERMINAL:
            raise InvalidTaskTransition(f"terminal task cannot transition: {task.status} -> {target}")
        if target not in _ALLOWED.get(task.status, frozenset()):
            raise InvalidTaskTransition(f"invalid task transition: {task.status} -> {target}")
        task.status = target
        return target
