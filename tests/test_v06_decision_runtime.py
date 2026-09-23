from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.orchestrator import SophiaCore
from sophia.decisions import DecisionManager, QwenFastDecisionProvider, RuleDecisionProvider


def test_invalid_fast_output_falls_back_to_local_rules():
    outcome = DecisionManager(QwenFastDecisionProvider(DummyBrain("not json")), RuleDecisionProvider()).decide("fix bug")
    assert outcome.provider == "rules" and outcome.task_type == "debug"


def test_confident_fast_output_is_used():
    output = '{"task_type":"architecture","complexity":0.8,"confidence":0.95,"reason":"review"}'
    outcome = DecisionManager(QwenFastDecisionProvider(DummyBrain(output)), RuleDecisionProvider()).decide("anything")
    assert outcome.provider == "qwen_fast" and outcome.task_type == "architecture"


def test_core_records_decision_runtime_event(tmp_path):
    fast = DummyBrain('{"task_type":"chat","complexity":0.2,"confidence":0.95,"reason":"simple"}')
    registry = BrainRegistry(); registry.register("fast", fast); registry.register("general", DummyBrain("ok")); registry.register("coder", DummyBrain("ok"))
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    task, _ = core.run("hello")
    assert core.runtime_events[task.id][0]["kind"] == "decision"
