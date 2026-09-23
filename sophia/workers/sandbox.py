from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sophia.workers.base import WorkerCall, WorkerOperation


WorkerHandler = Callable[[WorkerCall], Any]


class WorkerSandbox:
    """Capability boundary for one ephemeral worker invocation.

    A worker receives a host-registered callable only.  It has no core object,
    user channel, persistent memory, or method for spawning another worker.
    """

    allowed_operations = frozenset({
        WorkerOperation.SEARCH,
        WorkerOperation.LOG_ANALYSIS,
        WorkerOperation.INDEPENDENT_TEST,
    })

    def run(self, call: WorkerCall, handler: WorkerHandler) -> Any:
        if call.operation not in self.allowed_operations:
            raise PermissionError(f"worker operation requires sequential host execution: {call.operation}")
        return handler(call)
