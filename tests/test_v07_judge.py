from sophia.judge import JudgeVerdict, RuleJudge
from sophia.judge.layer import JudgeDecision
from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.orchestrator import SophiaCore
from sophia.runtime.task import TaskStatus


def test_rule_judge_validates_bounded_tool_worker_test_and_rag_evidence():
    judge = RuleJudge()
    assert judge.validate_tool_call(tool="filesystem.write", arguments={"path": "a.py"}).approved
    assert judge.validate_worker_result(result=[]).approved
    assert judge.validate_test_claim(passed=True, summary="1 passed", changed_path="a.py").approved
    assert judge.validate_groundedness(answer="answer", sources=("source",)).approved
    assert judge.validate_test_claim(passed=None, summary="No tests/ directory configured; test step skipped.", changed_path="a.py").verdict is JudgeVerdict.UNCERTAIN
    assert not judge.validate_test_claim(passed=None, summary="skipped", changed_path="a.py").approved
    assert not judge.validate_groundedness(answer="answer", sources=()).approved


class RejectingJudge:
    def validate_tool_call(self, **_kwargs): return JudgeDecision(True, "tool_call", "ok")
    def validate_worker_result(self, **_kwargs): return JudgeDecision(True, "worker_result", "ok")
    def validate_groundedness(self, **_kwargs): return JudgeDecision(True, "rag_groundedness", "ok")
    def validate_test_claim(self, **_kwargs): return JudgeDecision(False, "test_claim", "independent validation failed")


def test_judge_rejection_rolls_back_before_review_or_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.py").write_text("value = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"; tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    patch = """```diff
--- example.py
+++ example.py
@@ -1 +1 @@
-value = 1
+value = 2
```"""
    registry = BrainRegistry(); registry.register("coder", DummyBrain(patch)); registry.register("general", DummyBrain("review"))
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl", judge=RejectingJudge()).run("Update example.py")
    assert task.status is TaskStatus.FAILED and "judge rejected commit" in result.text
    assert (tmp_path / "example.py").read_text(encoding="utf-8") == "value = 1\n"
    checkpoint = next((tmp_path / ".sophia" / "checkpoints").glob("*/checkpoint.json"))
    assert '"status": "rolled_back"' in checkpoint.read_text(encoding="utf-8")
