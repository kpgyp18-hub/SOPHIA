from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4
from sophia.runtime.cancellation import CancellationToken


class WorkerOperation(StrEnum):
    SEARCH = "search"
    LOG_ANALYSIS = "log_analysis"
    INDEPENDENT_TEST = "independent_test"
    FILE_MODIFY = "file_modify"
    DB_MIGRATION = "db_migration"
    GIT_OPERATION = "git_operation"
    DESTRUCTIVE_TOOL = "destructive_tool"


PARALLEL_OPERATIONS = frozenset({
    WorkerOperation.SEARCH,
    WorkerOperation.LOG_ANALYSIS,
    WorkerOperation.INDEPENDENT_TEST,
})


@dataclass(frozen=True)
class WorkerCall:
    """Explicit, bounded worker input with recursive-call guard data."""

    worker_id: str
    root_task_id: str
    operation: WorkerOperation
    payload: Any = None
    call_id: str = field(default_factory=lambda: f"worker-{uuid4().hex[:12]}")
    parent_call_id: str | None = None
    depth: int = 0
    visited_providers: frozenset[str] = field(default_factory=frozenset)
    visited_workers: frozenset[str] = field(default_factory=frozenset)
    external_hops: int = 0
    cancellation: CancellationToken | None = None


class WorkerRejectedError(ValueError):
    """The pool rejected a call before a handler could execute."""
