import pytest

from sophia.database import DatabaseManager
from sophia.runtime.tool_registry import ToolRegistry, ToolSpec
from sophia.runtime.tools import ToolGateway
from sophia.skills import Skill, SkillExecutor, SkillRegistry
from sophia.workers import WorkerCall, WorkerOperation, WorkerPool


def _skills(tmp_path):
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    return SkillRegistry(database, library=tmp_path / "skills")


def test_skill_runtime_basic_searches_candidates_and_enforces_preconditions(tmp_path):
    registry = _skills(tmp_path)
    skill = registry.register(Skill(
        name="summarize-log", version="1", description="Summarize a local log file.", status="active",
        metadata={"required_inputs": ["task_id", "log"], "task_types": ["debug"]},
    ))
    executor = SkillExecutor(registry)
    executor.register_handler(skill.skill_id, lambda inputs: inputs["log"].upper())

    assert registry.search("summarize log") == [skill]
    assert executor.execute(skill.skill_id, {"task_id": "t1", "task_type": "debug"}).status == "rejected"
    result = executor.execute(skill.skill_id, {"task_id": "t1", "task_type": "debug", "log": "ready"})
    assert result.status == "completed" and result.result == "READY"


def test_worker_uses_tool_registry_and_denies_write_escalation(tmp_path):
    tools = ToolRegistry(ToolGateway(tmp_path))
    tools.register(ToolSpec("test.worker.r2", "write", "filesystem", "R2", 10, False, False, False,
                            frozenset({"aide"}), True))
    pool = WorkerPool(tool_registry=tools)
    pool.register_handler("search", lambda _call: None)
    call = WorkerCall("search", "task-1", WorkerOperation.SEARCH)
    assert pool.dispatch_tool(call, "filesystem.read", lambda invocation: invocation.spec.name) == "filesystem.read"
    called = False

    def handler(_invocation):
        nonlocal called
        called = True

    with pytest.raises(PermissionError, match="worker permission profile denies"):
        pool.dispatch_tool(call, "test.worker.r2", handler, request_context="write config")
    assert not called
