from __future__ import annotations

from dataclasses import dataclass, field

from sophia.arbitration.cooldown import CooldownTracker
from sophia.arbitration.edge import Edge, EdgeKind
from sophia.arbitration.modes import MODE_BIASES, ArbitrationMode
from sophia.arbitration.node import Node, NodeType


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class ArbitrationResult:
    mode: ArbitrationMode
    activations: dict[str, float]
    winners: tuple[str, ...]
    trace: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def activation(self, node_id: str) -> float:
        return self.activations.get(node_id, 0.0)


class ActivationGraph:
    def __init__(self, nodes: tuple[Node, ...] = (), edges: tuple[Edge, ...] = ()) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        for node in nodes:
            self.add_node(node)
        for edge in edges:
            self.add_edge(edge)

    def add_node(self, node: Node) -> None:
        if node.node_id in self.nodes:
            raise ValueError(f"duplicate arbitration node: {node.node_id}")
        self.nodes[node.node_id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            raise ValueError(f"edge references unknown node: {edge.source}->{edge.target}")
        self.edges.append(edge)

    def evaluate(self, inputs: dict[str, float], *, state: dict[str, float] | None = None,
                 mode: ArbitrationMode | str = ArbitrationMode.NORMAL,
                 cooldown: CooldownTracker | None = None, turn: int = 0,
                 cooldown_turns: dict[str, int] | None = None) -> ArbitrationResult:
        selected_mode = ArbitrationMode(mode)
        values = {identifier: _clamp(node.bias + float(inputs.get(identifier, 0.0)))
                  for identifier, node in self.nodes.items()}
        trace: dict[str, list[str]] = {identifier: [] for identifier in self.nodes}
        for identifier, bias in MODE_BIASES[selected_mode].items():
            if identifier in values:
                values[identifier] = _clamp(values[identifier] + bias)
                trace[identifier].append(f"mode.{selected_mode.value} {bias:+.3f}")
        for identifier, bias in (state or {}).items():
            if identifier in values:
                values[identifier] = _clamp(values[identifier] + float(bias))
                trace[identifier].append(f"state {float(bias):+.3f}")
        baseline = dict(values)
        for edge in self.edges:
            contribution = baseline[edge.source] * edge.signed_weight
            values[edge.target] = _clamp(values[edge.target] + contribution)
            if contribution:
                trace[edge.target].append(f"{edge.source} {contribution:+.3f}")
        if cooldown is not None:
            for identifier, node in self.nodes.items():
                penalty = cooldown.penalty(identifier, refractory_ms=node.refractory_ms, turn=turn,
                                           cooldown_turns=(cooldown_turns or {}).get(identifier, 0))
                if penalty:
                    values[identifier] = _clamp(values[identifier] - penalty)
                    trace[identifier].append(f"cooldown {-penalty:+.3f}")
        winners = tuple(identifier for identifier, node in self.nodes.items()
                        if node.node_type not in {NodeType.INPUT, NodeType.STATE, NodeType.MODE}
                        and values[identifier] >= node.threshold)
        winners = tuple(sorted(winners, key=lambda identifier: (-values[identifier], identifier)))
        return ArbitrationResult(selected_mode, values, winners,
                                 {key: tuple(items) for key, items in trace.items() if items})


def build_default_graph() -> ActivationGraph:
    nodes = tuple(
        [Node(f"feature.{name}", NodeType.INPUT, 1.0) for name in (
            "coding", "debugging", "reasoning", "planning", "review", "privacy", "risk", "urgency",
            "complexity", "tool_need", "memory_need", "vision_need", "external_need",
        )]
        + [Node(f"state.{name}", NodeType.STATE, 1.0) for name in (
            "provider_failure", "recent_tool_failure", "rollback_active", "workspace_stale",
            "memory_conflict", "high_load", "low_vram", "critical_mode", "user_waiting",
        )]
        + [Node("supervisor.aide", NodeType.SUPERVISOR, 0.55), Node("supervisor.blanc", NodeType.SUPERVISOR, 0.50)]
        + [Node(f"skill.{name}", NodeType.SKILL, 0.55) for name in ("diagnose", "reviewer", "architecture-review")]
        + [Node(f"action.{name}", NodeType.ACTION, 0.55, refractory_ms=3_000) for name in (
            "analyze", "implement", "review", "retrieve", "test", "escalate", "wait", "rollback",
        )]
        + [Node("safety.deep", NodeType.SAFETY, 0.80), Node("safety.checkpoint", NodeType.SAFETY, 0.55),
           Node("safety.approval", NodeType.SAFETY, 0.70), Node("memory.project", NodeType.MEMORY, 0.50),
           Node("provider.fast", NodeType.PROVIDER, 0.50), Node("provider.reasoning", NodeType.PROVIDER, 0.50),
           Node("provider.external", NodeType.PROVIDER, 0.75)]
    )
    edges = (
        Edge("feature.coding", "supervisor.aide", 0.8), Edge("feature.coding", "action.implement", 0.8),
        Edge("feature.debugging", "skill.diagnose", 0.9), Edge("feature.debugging", "supervisor.aide", 0.7),
        Edge("feature.reasoning", "supervisor.blanc", 0.8), Edge("feature.reasoning", "provider.reasoning", 0.8),
        Edge("feature.review", "skill.reviewer", 0.8), Edge("feature.review", "action.review", 0.8),
        Edge("feature.planning", "skill.architecture-review", 0.8), Edge("feature.planning", "supervisor.blanc", 0.7),
        Edge("feature.memory_need", "memory.project", 0.8), Edge("feature.memory_need", "action.retrieve", 0.7),
        Edge("feature.risk", "safety.checkpoint", 0.8), Edge("feature.risk", "safety.approval", 0.6),
        Edge("feature.external_need", "provider.external", 0.8),
        Edge("feature.urgency", "provider.fast", 0.6),
        Edge("feature.complexity", "provider.reasoning", 0.7),
        Edge("state.provider_failure", "action.escalate", 0.8),
        Edge("state.recent_tool_failure", "skill.diagnose", 0.7),
        Edge("state.rollback_active", "action.rollback", 0.9),
        Edge("state.workspace_stale", "action.wait", 0.9),
        Edge("state.low_vram", "provider.reasoning", 0.7, EdgeKind.INHIBITORY),
        Edge("state.critical_mode", "safety.deep", 0.9),
        Edge("state.critical_mode", "action.implement", 0.8, EdgeKind.INHIBITORY),
        Edge("feature.coding", "action.analyze", 0.35, EdgeKind.INHIBITORY),
        Edge("feature.risk", "action.implement", 0.45, EdgeKind.INHIBITORY),
    )
    return ActivationGraph(nodes, edges)
