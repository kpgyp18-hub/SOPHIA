from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class TaskType(StrEnum):
    CHAT = "chat"
    ARCHITECTURE = "architecture"
    CODING = "coding"
    DEBUG = "debug"
    REVIEW = "review"


class TaskStatus(StrEnum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    ROUTED = "routed"
    PLANNED = "planned"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    DIAGNOSING = "diagnosing"
    RETRY = "retry"
    SAFETY_HOLD = "safety_hold"
    USER_DECISION_REQUIRED = "user_decision_required"
    RECURSION_BLOCKED = "recursion_blocked"
    MAX_DEPTH_REACHED = "max_depth_reached"
    ROLLBACK = "rollback"
    PAUSED = "paused"
    ESCALATED = "escalated"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    CANCEL_FAILED = "cancel_failed"


@dataclass
class Task:
    input: str
    task_type: TaskType = TaskType.CHAT
    complexity: float = 0.25
    id: str = field(default_factory=lambda: f"task-{uuid4().hex[:12]}")
    status: TaskStatus = TaskStatus.RECEIVED
    brain_calls: int = 0
    retry_count: int = 0
    review_count: int = 0
    tool_calls: int = 0
