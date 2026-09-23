import json
import tempfile
import unittest
from pathlib import Path

from scripts.supervisor.call_graph import DIRECT_CODEX
from scripts.supervisor.codex_adapter import CodexCLIAdapter
from scripts.supervisor.fake_adapter import FakeAdapter
from scripts.supervisor.feature_builder import build_features, same_failure_count
from scripts.supervisor.model_budget import ModelBudget
from scripts.supervisor.model_config import ModelConfig, ModelConfigError
from scripts.supervisor.model_router import ModelRouter
from scripts.supervisor.supervisor import Supervisor

class CapturedRunner:
    def __init__(self, response=(0, '{"status":"OK","step_id":"01-exec"}', '')):
        self.response, self.calls = response, []
    def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None):
        self.calls.append(list(command)); return self.response

def features(**changes):
    value = {"task_type":"structured", "complexity":"normal", "requires_reasoning":True,
             "input_paths":[], "prior_model":"NONE", "test_available":True, "destructive":False,
             "risk":"low", "review_recommended":False}
    value.update(changes); return build_features(value)

class ModelRouterTests(unittest.TestCase):
    def setUp(self): self.router = ModelRouter()
    def test_luna(self): self.assertEqual(self.router.decide(features(complexity="simple", requires_reasoning=False, input_paths=["a"])).model_tier, "LUNA")
    def test_terra(self): self.assertEqual(self.router.decide(features()).model_tier, "TERRA")
    def test_sol_complex(self): self.assertEqual(self.router.decide(features(complexity="complex")).model_tier, "SOL")
    def test_sol_repeated_failure(self): self.assertEqual(self.router.decide({**features(), "same_failure_count": 2}).model_tier, "SOL")
    def test_missing_semantics_needs_arbiter(self): self.assertEqual(self.router.decide(features(complexity=None)).status, "NEED_ARBITER")
    def test_risk_and_destructive_do_not_escalate(self): self.assertEqual(self.router.decide(features(complexity="simple", requires_reasoning=False, destructive=True, risk="high")).model_tier, "LUNA")
    def test_deescalation_ignores_prior_model(self): self.assertEqual(self.router.decide(features(complexity="simple", requires_reasoning=False, prior_model="SOL")).model_tier, "LUNA")
    def test_failure_signatures_do_not_combine(self):
        history = [{"step_scope":"s", "reason_code":"A"}, {"step_scope":"s", "reason_code":"B"}]
        self.assertEqual(same_failure_count(history, "s", "A", None), 1)
    def test_matching_failure_counts(self):
        history = [{"step_scope":"s", "validation_failure_signature":"x"}] * 2
        self.assertEqual(same_failure_count(history, "s", None, "x"), 2)
    def test_budget_and_gate(self):
        budget = ModelBudget(max_luna_calls=1)
        self.assertFalse(budget.can_execute("LUNA", global_budget_pass=False)); budget.record_call("LUNA")
        self.assertFalse(budget.can_execute("LUNA", global_budget_pass=True))
    def test_config_mapping_and_missing(self):
        config = ModelConfig.from_mapping({"codex":{"luna":"a","terra":"b","sol":"c"}})
        self.assertEqual(config.resolve("TERRA"), "b")
        with self.assertRaises(ModelConfigError): ModelConfig.from_mapping({"codex":{"luna":"a"}})
    def test_adapter_resolves_model_to_argv(self):
        config = ModelConfig.from_mapping({"codex":{"luna":"a","terra":"b","sol":"c"}})
        runner = CapturedRunner(); adapter = CodexCLIAdapter(runner, executable_path="mock-codex", model_config=config)
        adapter.execute({"step_id":"01-exec", "role":"exec", "required_skills":[], "input_paths":[], "previous_step":"NONE", "return_to":"NONE", "model_tier":"TERRA"})
        self.assertEqual(runner.calls[0][-2:], ["--model", "b"])
    def test_supervisor_records_tier_only(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(Path(directory)/"runtime", Path(directory)/"skills", {"CODEX": FakeAdapter([{"status":"OK","step_id":"01-exec"}])}, model_router=self.router, codex_feature_inputs={"*": {"complexity":"normal", "requires_reasoning":True, "task_type":"structured"}})
            state = supervisor.run("task-001", DIRECT_CODEX)
            task = supervisor.store.read_step("task-001", 1, "exec", "task")
        self.assertEqual(state["status"], "DONE"); self.assertEqual(task["model_tier"], "TERRA")
        self.assertNotIn("backend_model", json.dumps(task))
    def test_global_budget_blocks_before_adapter(self):
        adapter = FakeAdapter([{"status":"OK","step_id":"01-exec"}])
        with tempfile.TemporaryDirectory() as directory:
            state = Supervisor(Path(directory)/"runtime", Path(directory)/"skills", {"CODEX": adapter}, model_router=self.router, codex_feature_inputs={"*": {"complexity":"normal", "requires_reasoning":True}}, global_budget_check=lambda _: False).run("task-001", DIRECT_CODEX)
        self.assertEqual(state["status"], "NEED_USER"); self.assertEqual(adapter.calls, [])

if __name__ == '__main__': unittest.main()