from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from sophia.workers.base import WorkerCall, WorkerRejectedError
from sophia.workers.result import WorkerResult, WorkerStatus
from sophia.workers.sandbox import WorkerHandler, WorkerSandbox
from sophia.workers.scheduler import WorkerScheduler
from sophia.runtime.tool_registry import ToolInvocation, ToolRegistry
from sophia.runtime.cancellation import TaskCancelledError


class WorkerPool:
    """Bounded, handler-only execution for disposable read-only workers."""

    def __init__(self, *, max_worker_depth: int = 1, max_worker_count: int = 3,
                 max_worker_retry: int = 1, tool_registry: ToolRegistry | None = None,
                 worker_r1_allowlist: frozenset[str] = frozenset()) -> None:
        if min(max_worker_depth, max_worker_count, max_worker_retry) < 0 or max_worker_count < 1:
            raise ValueError("worker limits must be non-negative and worker count must be positive")
        self.max_worker_depth = max_worker_depth
        self.max_worker_count = max_worker_count
        self.max_worker_retry = max_worker_retry
        self._handlers: dict[str, WorkerHandler] = {}
        self._active = 0
        self._lock = Lock()
        self.scheduler = WorkerScheduler()
        self.tool_registry = tool_registry
        self.worker_r1_allowlist = worker_r1_allowlist

    def register_handler(self, worker_id: str, handler: WorkerHandler) -> None:
        if not worker_id or not callable(handler):
            raise ValueError("worker_id and callable handler are required")
        self._handlers[worker_id] = handler

    def unregister_handler(self, worker_id: str) -> None:
        self._handlers.pop(worker_id, None)

    def dispatch_tool(self, call: WorkerCall, name: str, handler: Callable[[ToolInvocation], Any], *,
                      request_context: str = "", supervisor: str = "aide") -> Any:
        """Execute a worker tool only through the shared Registry dispatch path.

        Workers keep the conservative profile from v0.7.1: R0 is allowed,
        explicitly named R1 capabilities may be allowed, and every R2+ request
        is denied before its host-owned handler can run.
        """
        self._validate(call)
        if call.cancellation is not None:
            call.cancellation.raise_if_cancelled()
        if self.tool_registry is None:
            raise PermissionError("worker tool dispatch is unavailable without a ToolRegistry")

        def guarded(invocation: ToolInvocation) -> Any:
            risk = invocation.effective_risk or invocation.spec.risk
            if risk == "R0":
                return handler(invocation)
            if risk == "R1" and invocation.spec.name in self.worker_r1_allowlist:
                return handler(invocation)
            raise PermissionError(f"worker permission profile denies {invocation.spec.name} at {risk}")

        return self.tool_registry.dispatch(name, supervisor, guarded, worker=True,
                                           request_context=request_context, task_id=call.root_task_id,
                                           cancellation=call.cancellation)

    def _validate(self, call: WorkerCall) -> WorkerHandler:
        if call.depth > self.max_worker_depth:
            raise WorkerRejectedError(f"worker depth exceeds {self.max_worker_depth}")
        if call.worker_id in call.visited_workers:
            raise WorkerRejectedError(f"recursive worker call rejected: {call.worker_id}")
        if call.external_hops < 0:
            raise WorkerRejectedError("external_hops cannot be negative")
        if call.operation not in WorkerSandbox.allowed_operations:
            raise WorkerRejectedError(f"worker operation requires sequential host execution: {call.operation}")
        handler = self._handlers.get(call.worker_id)
        if handler is None:
            raise WorkerRejectedError(f"no host handler registered for worker: {call.worker_id}")
        return handler

    def run(self, call: WorkerCall) -> WorkerResult:
        try:
            if call.cancellation is not None:
                call.cancellation.raise_if_cancelled()
            handler = self._validate(call)
            with self._lock:
                if self._active >= self.max_worker_count:
                    raise WorkerRejectedError(f"worker capacity reached ({self.max_worker_count})")
                self._active += 1
        except (ValueError, PermissionError, TaskCancelledError) as error:
            return WorkerResult(call.worker_id, call.call_id, WorkerStatus.REJECTED, error=str(error))
        try:
            for attempt in range(self.max_worker_retry + 1):
                try:
                    if call.cancellation is not None:
                        call.cancellation.raise_if_cancelled()
                    value = WorkerSandbox().run(call, handler)
                    return WorkerResult(call.worker_id, call.call_id, WorkerStatus.COMPLETED,
                                        value=value, attempts=attempt + 1,
                                        metadata={"root_task_id": call.root_task_id, "depth": call.depth})
                except Exception as error:  # handler faults are contained and bounded
                    if attempt == self.max_worker_retry:
                        return WorkerResult(call.worker_id, call.call_id, WorkerStatus.FAILED,
                                            error=str(error), attempts=attempt + 1)
        finally:
            with self._lock:
                self._active -= 1
        raise AssertionError("unreachable worker result state")

    def run_many(self, calls: Iterable[WorkerCall]) -> list[WorkerResult]:
        parallel, sequential = self.scheduler.partition(calls)
        results: list[WorkerResult] = []
        if parallel:
            with ThreadPoolExecutor(max_workers=self.max_worker_count) as executor:
                results.extend(executor.map(self.run, parallel))
        # Operations outside the sandbox retain deterministic host serialization.
        results.extend(self.run(call) for call in sequential)
        return results
