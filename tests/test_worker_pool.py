from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.orchestrator import SophiaCore
from sophia.workers import WorkerCall, WorkerOperation, WorkerPool, WorkerStatus


def test_worker_pool_runs_only_host_registered_read_only_handlers():
    pool = WorkerPool()
    pool.register_handler("search", lambda call: {"query": call.payload, "hits": 1})
    result = pool.run(WorkerCall("search", "task-1", WorkerOperation.SEARCH, "SOPHIA"))
    assert result.status is WorkerStatus.COMPLETED
    assert result.value == {"query": "SOPHIA", "hits": 1}
    assert result.metadata == {"root_task_id": "task-1", "depth": 0}


def test_worker_pool_rejects_unregistered_recursive_and_mutating_calls():
    pool = WorkerPool()
    missing = pool.run(WorkerCall("missing", "task-1", WorkerOperation.SEARCH))
    assert missing.status is WorkerStatus.REJECTED
    pool.register_handler("search", lambda call: "unexpected")
    recursive = pool.run(WorkerCall("search", "task-1", WorkerOperation.SEARCH, visited_workers=frozenset({"search"})))
    assert recursive.status is WorkerStatus.REJECTED
    mutable = pool.run(WorkerCall("search", "task-1", WorkerOperation.FILE_MODIFY))
    assert mutable.status is WorkerStatus.REJECTED


def test_worker_pool_retries_faults_once_and_serializes_non_parallel_work():
    calls = {"count": 0}
    def flaky(call):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary")
        return "ok"
    pool = WorkerPool(max_worker_retry=1)
    pool.register_handler("test", flaky)
    result = pool.run(WorkerCall("test", "task-1", WorkerOperation.INDEPENDENT_TEST))
    assert result.status is WorkerStatus.COMPLETED and result.attempts == 2
    results = pool.run_many([WorkerCall("test", "task-1", WorkerOperation.INDEPENDENT_TEST),
                             WorkerCall("test", "task-1", WorkerOperation.GIT_OPERATION)])
    assert [item.status for item in results] == [WorkerStatus.COMPLETED, WorkerStatus.REJECTED]


def test_core_exposes_a_bounded_worker_pool(tmp_path):
    registry = BrainRegistry(); registry.register("general", DummyBrain()); registry.register("coder", DummyBrain())
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    assert core.worker_pool.max_worker_depth == 1
    assert core.worker_pool.max_worker_count == 3
