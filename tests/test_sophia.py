from sophia.brains.ollama import BrainRegistry, BrainResult
from sophia.core.orchestrator import SophiaCore


class StubBrain:
    def generate(self, prompt: str) -> BrainResult:
        return BrainResult("ok", 2, 1, 3)


def test_routes_coding_to_aide_and_coder(tmp_path):
    registry = BrainRegistry(); registry.register("general", StubBrain()); registry.register("coder", StubBrain())
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("코드를 수정하고 테스트해줘")
    assert task.task_type == "coding"
    assert result.text == "ok"


def test_router_persists_single_task_decision(tmp_path):
    from sophia.runtime.store import TaskStore
    registry = BrainRegistry(); registry.register("general", StubBrain()); registry.register("coder", StubBrain())
    task, _ = SophiaCore(registry, tmp_path / "telemetry.jsonl", TaskStore(tmp_path / "tasks")).run("프로젝트 구조를 설계해줘")
    state = (tmp_path / "tasks" / f"{task.id}.json").read_text(encoding="utf-8")
    assert '"supervisor": "blanc"' in state


def test_chat_task_tracker_records_routing_replies_and_completion(tmp_path):
    from sophia.runtime.store import TaskStore
    from sophia.runtime.tracker import ChatTaskTracker

    tracker = ChatTaskTracker("phone", TaskStore(tmp_path / "tasks"))
    task_id = tracker.begin("debug this code")
    tracker.reply(task_id, "Codex", "fixed")
    tracker.complete(task_id)

    state = tracker.store.read(task_id)
    assert state is not None
    assert state["source"] == "phone"
    assert state["brain"] == "coder"
    assert state["status"] == "completed"
    assert [event["type"] for event in state["events"]] == ["received", "reply", "completed"]


def test_chat_task_tracker_records_explicit_handoff(tmp_path):
    from sophia.runtime.store import TaskStore
    from sophia.runtime.tracker import ChatTaskTracker

    tracker = ChatTaskTracker("pc-cli", TaskStore(tmp_path / "tasks"))
    task_id = tracker.begin("review this implementation")
    tracker.handoff(task_id, "Claude", "Codex", 1)
    state = tracker.store.read(task_id)
    assert state is not None
    assert state["handoff"] == {"from": "Claude", "to": "Codex", "hop": 1}
    assert state["events"][-1]["type"] == "handoff"


def test_escalation_requires_approval_before_external_model_use():
    from sophia.core.router import Router

    decision = Router().decide("Use an external model for this task")
    assert decision.escalation.route == "user_approval"
    assert decision.escalation.requires_user_approval is True


def test_escalation_picks_codex_tier_via_existing_model_router():
    from sophia.core.escalation import EscalationPolicy
    from sophia.runtime.task import Task, TaskType

    architecture_task = Task("외부 모델로 이 설계를 검토해줘", TaskType.ARCHITECTURE, 0.6)
    decision = EscalationPolicy().plan(architecture_task)
    assert decision.route == "user_approval"
    assert decision.suggested_tier == "TERRA"

    high_complexity_task = Task("외부 모델 호출", TaskType.CHAT, 0.9)
    assert EscalationPolicy().plan(high_complexity_task).suggested_tier == "SOL"


def test_failed_coding_chat_escalates_to_blanc_review(tmp_path):
    from sophia.runtime.store import TaskStore
    from sophia.runtime.tracker import ChatTaskTracker

    tracker = ChatTaskTracker("pc-cli", TaskStore(tmp_path / "tasks"))
    task_id = tracker.begin("debug this code")
    tracker.reply(task_id, "Codex", "(error: test failed)")

    state = tracker.store.read(task_id)
    assert state is not None
    assert state["escalation"]["route"] == "dual_review"
    assert state["escalation"]["next_supervisor"] == "blanc"
    tracker.complete(task_id)
    state = tracker.store.read(task_id)
    assert state is not None
    assert state["status"] == "escalated"
    assert state["events"][-1]["type"] == "awaiting_handoff"


def test_context_manager_compacts_old_turns_and_keeps_recent_context(tmp_path):
    from sophia.core.context_manager import ContextManager

    manager = ContextManager(tmp_path / "context.json", recent_turns=2, max_recent_chars=100, max_summary_chars=100)
    manager.add("task-one", "first request", "first result")
    manager.add("task-two", "second request", "second result")

    context = manager.build()
    assert "Compressed earlier context" in context
    assert "first request" in context
    assert "second result" in context
    assert len(manager._load()["turns"]) == 2


def test_core_passes_bounded_prior_context_to_the_next_task(tmp_path):
    class CapturingBrain(StubBrain):
        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str) -> BrainResult:
            self.prompts.append(prompt)
            return BrainResult("ok", 2, 1, 3)

    brain = CapturingBrain()
    registry = BrainRegistry(); registry.register("general", brain); registry.register("coder", brain)
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    core.run("first conversation")
    core.run("second conversation")
    assert "Recent shared context" in brain.prompts[-1]
    assert "first conversation" in brain.prompts[-1]


def test_task_store_returns_newest_task_snapshots(tmp_path):
    from sophia.runtime.store import TaskStore

    store = TaskStore(tmp_path / "tasks")
    store.write("task-old", status="completed")
    store.write("task-new", status="executing")
    assert [task["task_id"] for task in store.recent()] == ["task-new", "task-old"]


def test_cli_status_and_telemetry_show_v03_runtime_state(tmp_path):
    from sophia.cli.sophia import status_lines, telemetry_summary
    registry = BrainRegistry(); registry.register("general", StubBrain()); registry.register("coder", StubBrain())
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    assert "CORE        ONLINE" in status_lines(core)
    core.run("ordinary chat")
    assert "Brain Calls: 1" in telemetry_summary(core)


def test_supervisor_identity_is_preserved_when_brains_are_replaced():
    from sophia.brains.dummy import DummyBrain
    from sophia.runtime.task import Task, TaskType
    from sophia.supervisors.supervisors import Aide, Blanc

    aide_brain, blanc_brain = DummyBrain(), DummyBrain()
    task = Task("review this", TaskType.CODING)
    Aide().run(task, aide_brain, "context")
    Blanc().run(task, blanc_brain, "context")
    assert "You are Aide." in aide_brain.prompts[0]
    assert "Execute the request precisely." in aide_brain.prompts[0]
    assert "You are Blanc." in blanc_brain.prompts[0]
    assert "Plan, assess risk" in blanc_brain.prompts[0]


def test_database_manager_uses_sqlite_fallback_and_round_trips_backup(tmp_path):
    from sophia.database import DatabaseManager

    manager = DatabaseManager({"active": "postgresql", "fallback": "sqlite",
                               "backends": {
                                   # An unused local port fails deterministically, even when a
                                   # developer has PostgreSQL running on the default port.
                                   "postgresql": {"host": "127.0.0.1", "port": 1},
                                   "sqlite": {"path": str(tmp_path / "sophia.db")},
                               }})
    assert manager.record("task", {"id": "one"}) == "sqlite"
    backup = manager.backup(tmp_path / "backup.json")
    assert backup.is_file()
    assert '"id": "one"' in backup.read_text(encoding="utf-8")
    assert manager.restore(backup) == "sqlite"


def test_cli_brain_blocks_self_reference_before_calling_transport():
    from sophia.brains.cli import CliBrain, SelfReferenceError

    called = False
    def transport(prompt):
        nonlocal called
        called = True
        return BrainResult("unreachable")

    with __import__("pytest").raises(SelfReferenceError):
        CliBrain("codex", transport, current_persona="codex").generate("anything")
    assert called is False


def test_prior_coding_task_makes_a_filename_free_followup_a_file_continuation(tmp_path, monkeypatch):
    from sophia.brains.dummy import DummyBrain

    monkeypatch.chdir(tmp_path)
    registry = BrainRegistry()
    registry.register("general", DummyBrain("ok"))
    registry.register("coder", DummyBrain("```python\ndef add(a, b):\n    return a + b\n```"))
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    first, _ = core.run("Create calculator.py")
    assert first.status.value == "completed"
    registry.register("coder", DummyBrain("```diff\n--- calculator.py\n+++ calculator.py\n@@ -1,2 +1,5 @@\n def add(a, b):\n     return a + b\n+\n+def subtract(a, b):\n+    return a - b\n```"))
    second, _ = core.run("multiply too")
    assert second.task_type.value == "coding"
    assert "subtract" in (tmp_path / "calculator.py").read_text(encoding="utf-8")
