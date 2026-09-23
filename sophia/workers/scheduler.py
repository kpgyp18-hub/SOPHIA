from __future__ import annotations

from collections.abc import Iterable

from sophia.workers.base import PARALLEL_OPERATIONS, WorkerCall


class WorkerScheduler:
    """Keeps mutating or destructive work out of parallel worker batches."""

    @staticmethod
    def partition(calls: Iterable[WorkerCall]) -> tuple[list[WorkerCall], list[WorkerCall]]:
        parallel, sequential = [], []
        for call in calls:
            (parallel if call.operation in PARALLEL_OPERATIONS else sequential).append(call)
        return parallel, sequential
