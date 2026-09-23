from sophia.core.state_machine import TaskStateMachine
from sophia.runtime.cancellation import CancellationRegistry, TaskCancelledError
from sophia.runtime.task import Task, TaskStatus
from sophia.runtime.tool_registry import ToolRegistry
from sophia.runtime.tools import ToolGateway
from sophia.workers import WorkerCall, WorkerOperation, WorkerPool, WorkerStatus


def test_v071_r2_tool_idempotency_returns_the_first_completed_result(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    calls = []

    def handler(_invocation):
        calls.append("run")
        return {"result": "completed"}

    first = registry.dispatch("database", "aide", handler, task_id="task-1", idempotency_key="retry-1")
    second = registry.dispatch("database", "aide", handler, task_id="task-1", idempotency_key="retry-1")
    assert first == second == {"result": "completed"}
    assert calls == ["run"]


def test_v071_cancellation_propagates_through_explicit_task_states():
    task = Task("cancel me")
    machine = TaskStateMachine()
    token = CancellationRegistry().create(task.id)
    token.cancel()
    try:
        token.raise_if_cancelled()
    except TaskCancelledError:
        machine.transition(task, TaskStatus.CLASSIFIED)
        machine.transition(task, TaskStatus.ROUTED)
        machine.transition(task, TaskStatus.PLANNED)
        machine.transition(task, TaskStatus.EXECUTING)
        machine.transition(task, TaskStatus.CANCEL_REQUESTED)
        machine.transition(task, TaskStatus.CANCELLING)
        machine.transition(task, TaskStatus.CANCELLED)
    assert task.status is TaskStatus.CANCELLED


def test_v071_cancellation_blocks_worker_and_tool_before_handler_execution(tmp_path):
    cancellation = CancellationRegistry()
    token = cancellation.create("task-1")
    token.cancel()
    pool = WorkerPool(tool_registry=ToolRegistry(ToolGateway(tmp_path)))
    pool.register_handler("search", lambda _call: "unexpected")
    result = pool.run(WorkerCall("search", "task-1", WorkerOperation.SEARCH, cancellation=token))
    assert result.status is WorkerStatus.REJECTED
    assert "CANCELLED" in (result.error or "")
