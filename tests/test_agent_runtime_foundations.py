from sophia.core.invocation_guard import InvocationGuard, InvocationDepthError, RecursiveInvocationError
from sophia.core.provider_resolver import ProviderResolver
from sophia.core.state_machine import InvalidTaskTransition, TaskStateMachine
from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.escalation import EscalationRoute
from sophia.runtime.task import Task, TaskStatus
from sophia.runtime.tool_registry import ToolRegistry
from sophia.runtime.tools import ToolGateway
from sophia.core.call_graph import CallGraph
from sophia.core.orchestrator import SophiaCore
from sophia.database import DatabaseManager
from sophia.skills import Skill


def test_state_machine_enforces_full_happy_path():
    task, machine = Task("test"), TaskStateMachine()
    for status in (TaskStatus.CLASSIFIED, TaskStatus.ROUTED, TaskStatus.PLANNED,
                   TaskStatus.EXECUTING, TaskStatus.VERIFYING, TaskStatus.REVIEWING,
                   TaskStatus.COMPLETED):
        machine.transition(task, status)
    assert task.status is TaskStatus.COMPLETED
    try:
        machine.transition(task, TaskStatus.EXECUTING)
    except InvalidTaskTransition:
        pass
    else:
        raise AssertionError("terminal task was revived")


def test_invocation_guard_blocks_active_recursion_and_depth():
    guard = InvocationGuard(max_depth=1)
    guard.enter("ollama", worker="aide")
    try:
        guard.enter("ollama", worker="aide")
    except (RecursiveInvocationError, InvocationDepthError):
        pass
    else:
        raise AssertionError("recursive call was accepted")
    guard.leave("ollama", worker="aide")


def test_provider_resolver_keeps_external_routes_approval_gated():
    registry = BrainRegistry()
    registry.register("general", DummyBrain("ok"))
    selection = ProviderResolver(registry).resolve(Task("external model"), "general",
                                                    route=EscalationRoute.USER_APPROVAL)
    assert selection.requires_user_approval is True


def test_tool_registry_enforces_supervisor_and_worker_boundaries(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    assert registry.authorize("filesystem.read", "blanc", worker=True).operation == "read"
    try:
        registry.authorize("filesystem.write", "blanc")
    except PermissionError:
        pass
    else:
        raise AssertionError("Blanc received an Aide-only write tool")


def test_call_graph_records_a_finished_provider_call():
    graph = CallGraph()
    graph.start("call-one", task_id="task-one", supervisor="aide", provider="ollama", brain="coder")
    graph.finish("call-one", status="completed")
    assert graph.snapshot("task-one")[0]["status"] == "completed"


def test_core_uses_worker_and_explicit_skill_runtime_and_records_events(tmp_path):
    registry = BrainRegistry(); registry.register("general", DummyBrain("ok")); registry.register("coder", DummyBrain("ok"))
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl", database=database)
    skill = core.skill_registry.register(Skill(name="double", version="1", description="Double input.", status="active"))
    core.register_skill_handler("double", lambda inputs: inputs["task_id"])
    task, _ = core.run("use skill:double")
    events = core.runtime_events[task.id]
    assert any(event["kind"] == "worker" and event["status"] == "completed" for event in events)
    assert any(event["kind"] == "skill" and event["skill_id"] == skill.skill_id and event["status"] == "completed" for event in events)


def test_invocation_guard_exposes_root_and_parent_call_metadata():
    guard = InvocationGuard(max_depth=2)
    root = guard.enter("one", worker="aide", root_task_id="task-1")
    child = guard.enter("two", worker="blanc", root_task_id="task-1")
    snapshot = guard.snapshot()
    assert snapshot[0]["root_task_id"] == "task-1"
    assert snapshot[1]["parent_call_id"] == root and snapshot[1]["call_id"] == child
    guard.leave("two", worker="blanc")
    guard.leave("one", worker="aide")


def test_core_routes_runtime_exceptions_through_validated_terminal_states(tmp_path):
    class FailingBrain:
        provider = "offline"
        def generate(self, prompt):
            raise OSError("offline")

    registry = BrainRegistry(); registry.register("general", FailingBrain()); registry.register("coder", FailingBrain())
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl",
                      database=DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path))
    task, result = core.run("ordinary chat")
    assert task.status is TaskStatus.PAUSED and "TASK_PAUSED" in result.text
