from __future__ import annotations

import json

import pytest

from sophia.arbitration import (
    ActivationGraph, ArbitrationMode, ArbitrationTelemetry, CooldownTracker, Edge, EdgeKind,
    NeuralArbitrator, Node, NodeType, RewardUpdater,
)
from sophia.core.router import Router
from sophia.runtime.task import Task, TaskType
from sophia.runtime.tool_registry import ToolRegistry
from sophia.runtime.tools import ToolGateway


def test_activation_threshold_and_cooperative_winners():
    graph = ActivationGraph(
        (Node("input", NodeType.INPUT, 1.0), Node("implement", NodeType.ACTION, 0.5),
         Node("test", NodeType.ACTION, 0.5)),
        (Edge("input", "implement", 0.7), Edge("input", "test", 0.6)),
    )
    result = graph.evaluate({"input": 1.0})
    assert result.winners == ("implement", "test")
    assert result.activation("implement") == pytest.approx(0.7)


def test_lateral_inhibition_suppresses_competitor():
    graph = ActivationGraph(
        (Node("fast", NodeType.INPUT, 1.0), Node("deep", NodeType.PROVIDER, 0.5, bias=0.7)),
        (Edge("fast", "deep", 0.4, EdgeKind.INHIBITORY),),
    )
    result = graph.evaluate({"fast": 1.0})
    assert result.activation("deep") == pytest.approx(0.3)
    assert "deep" not in result.winners


def test_refractory_period_and_mode_modulation():
    now = [1.0]
    cooldown = CooldownTracker(clock=lambda: now[0])
    graph = ActivationGraph((Node("provider.fast", NodeType.PROVIDER, 0.5, bias=0.4, refractory_ms=1000),))
    normal = graph.evaluate({}, cooldown=cooldown)
    cooldown.mark("provider.fast")
    blocked = graph.evaluate({}, cooldown=cooldown)
    fast = graph.evaluate({}, mode=ArbitrationMode.FAST)
    assert "provider.fast" not in normal.winners
    assert blocked.activation("provider.fast") == 0.0
    assert fast.activation("provider.fast") == pytest.approx(0.6)


def test_supervisor_hint_never_overrides_router_mapping():
    task = Task("architecture", TaskType.ARCHITECTURE, 0.9)
    result = NeuralArbitrator.default(enabled=True, operation="bias").recommend(task, task.input)
    assert result is not None
    assert Router.supervisor(task) == "blanc"


def test_arbitration_cannot_override_tool_permission(tmp_path):
    arbiter = NeuralArbitrator.default(enabled=True, operation="bias")
    arbiter.recommend(Task("delete", TaskType.CODING), "delete file")
    registry = ToolRegistry(ToolGateway(tmp_path))
    with pytest.raises(PermissionError):
        registry.prepare("filesystem.delete", "aide", request_context="delete file")


def test_reward_update_minimum_samples_bounds_and_decay():
    updater = RewardUpdater(learning_rate=0.1, minimum_samples=3, min_weight=-0.5, max_weight=0.5)
    edge = Edge("feature.x", "skill.x", 0.45)
    assert updater.update(edge, 1.0).weight == 0.45
    assert updater.update(edge, 1.0).weight == 0.45
    edge = updater.update(edge, 1.0)
    assert edge.weight == 0.5
    assert updater.decay(edge, 0.5).weight == 0.25
    protected = Edge("feature.x", "safety.deep", 0.2)
    assert updater.update(protected, 1.0).weight == 0.2


def test_shadow_mode_has_no_bias_and_bias_mode_is_limited():
    task = Task("urgent code", TaskType.CODING, 0.4)
    shadow = NeuralArbitrator.default(enabled=True, operation="shadow")
    shadow_result = shadow.recommend(task, "urgent code")
    assert shadow.provider_biases(shadow_result) == {}
    bias = NeuralArbitrator.default(enabled=True, operation="bias")
    values = bias.provider_biases(bias.recommend(task, "urgent code"))
    assert values and all(0.0 <= value <= 0.15 for value in values.values())


def test_failure_skill_metric_and_resource_state_biases_are_bounded():
    arbiter = NeuralArbitrator.default(enabled=True)
    task = Task("debug", TaskType.DEBUG, 0.7)
    baseline = arbiter.recommend(task, "debug")
    result = arbiter.recommend(task, "debug", state={
        "state.provider_failure": 1.0,
        "state.low_vram": 1.0,
        "skill.diagnose": 0.15,
    })
    assert result is not None
    assert result.activation("action.escalate") == pytest.approx(0.8)
    assert baseline is not None and result.activation("provider.reasoning") < baseline.activation("provider.reasoning")
    assert result.activation("skill.diagnose") == 1.0


def test_reward_feedback_is_connected_but_disabled_by_default():
    task = Task("debug", TaskType.DEBUG, 0.7)
    shadow = NeuralArbitrator.default(enabled=True)
    shadow.recommend(task, "debug")
    assert shadow.feedback("PASS") == 0
    learning = NeuralArbitrator.default(
        enabled=True, learning_enabled=True,
        reward_updater=RewardUpdater(learning_rate=0.1, minimum_samples=1),
    )
    learning.recommend(task, "debug")
    assert learning.feedback("PASS") > 0


def test_arbitration_trace_is_explainable_and_persisted(tmp_path):
    telemetry = ArbitrationTelemetry(tmp_path / "arbitration.jsonl")
    arbiter = NeuralArbitrator.default(enabled=True, telemetry=telemetry)
    result = arbiter.recommend(Task("debug code", TaskType.DEBUG, 0.7), "debug code", actual_route="aide")
    assert result is not None and result.trace["skill.diagnose"]
    rows = [json.loads(line) for line in (tmp_path / "arbitration.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["winner"] and row["task_id"] for row in rows)
