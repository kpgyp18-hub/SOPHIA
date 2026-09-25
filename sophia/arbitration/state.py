from __future__ import annotations

from dataclasses import dataclass, field

from sophia.arbitration.backend import ArbitrationBackend, DeterministicBackend
from sophia.arbitration.cooldown import CooldownTracker
from sophia.arbitration.graph import ArbitrationResult, build_default_graph
from sophia.arbitration.modes import ArbitrationMode
from sophia.arbitration.telemetry import ArbitrationTelemetry
from sophia.arbitration.scorer import RewardUpdater
from sophia.runtime.task import Task, TaskType


class FeatureExtractor:
    def extract(self, task: Task, text: str) -> dict[str, float]:
        lowered = text.casefold()
        feature = {f"feature.{name}": 0.0 for name in (
            "coding", "debugging", "reasoning", "planning", "review", "privacy", "risk", "urgency",
            "complexity", "tool_need", "memory_need", "vision_need", "external_need",
        )}
        feature["feature.coding"] = 1.0 if task.task_type in {TaskType.CODING, TaskType.DEBUG} else 0.0
        feature["feature.debugging"] = 1.0 if task.task_type is TaskType.DEBUG else 0.0
        feature["feature.reasoning"] = 1.0 if task.task_type in {TaskType.ARCHITECTURE, TaskType.REVIEW} else task.complexity
        feature["feature.planning"] = 1.0 if task.task_type is TaskType.ARCHITECTURE else 0.0
        feature["feature.review"] = 1.0 if task.task_type is TaskType.REVIEW else 0.0
        feature["feature.complexity"] = max(0.0, min(1.0, task.complexity))
        feature["feature.tool_need"] = 0.9 if any(term in lowered for term in ("실행", "수정", "write", "run")) else 0.0
        feature["feature.memory_need"] = 0.8 if any(term in lowered for term in ("기억", "이전", "memory", "history")) else 0.0
        feature["feature.vision_need"] = 0.9 if any(term in lowered for term in ("이미지", "사진", "image", "vision")) else 0.0
        feature["feature.external_need"] = 0.9 if any(term in lowered for term in ("외부", "external", "cloud")) else 0.0
        feature["feature.privacy"] = 0.9 if any(term in lowered for term in ("비밀", "개인정보", "secret", "private")) else 0.0
        feature["feature.risk"] = 0.9 if any(term in lowered for term in ("삭제", "drop", "reset --hard", "deploy")) else 0.0
        feature["feature.urgency"] = 0.8 if any(term in lowered for term in ("빨리", "긴급", "urgent", "asap")) else 0.0
        return feature


@dataclass
class NeuralArbitrator:
    backend: ArbitrationBackend
    enabled: bool = False
    operation: str = "shadow"
    mode: ArbitrationMode = ArbitrationMode.NORMAL
    extractor: FeatureExtractor = field(default_factory=FeatureExtractor)
    cooldown: CooldownTracker = field(default_factory=CooldownTracker)
    telemetry: ArbitrationTelemetry | None = None
    reward_updater: RewardUpdater = field(default_factory=RewardUpdater)
    learning_enabled: bool = False
    turn: int = 0
    _last_result: ArbitrationResult | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.operation not in {"shadow", "bias"}:
            raise ValueError("v0.8.x permits only shadow or bias operation")

    @classmethod
    def default(cls, *, enabled: bool = False, operation: str = "shadow",
                mode: ArbitrationMode | str = ArbitrationMode.NORMAL,
                telemetry: ArbitrationTelemetry | None = None,
                learning_enabled: bool = False,
                reward_updater: RewardUpdater | None = None) -> "NeuralArbitrator":
        return cls(DeterministicBackend(build_default_graph()), enabled, operation,
                   ArbitrationMode(mode), telemetry=telemetry,
                   reward_updater=reward_updater or RewardUpdater(), learning_enabled=learning_enabled)

    def recommend(self, task: Task, text: str, *, state: dict[str, float] | None = None,
                  actual_route: str | None = None) -> ArbitrationResult | None:
        if not self.enabled:
            return None
        self.turn += 1
        result = self.backend.evaluate(self.extractor.extract(task, text), state=state or {},
                                       mode=self.mode, cooldown=self.cooldown, turn=self.turn)
        for winner in result.winners:
            self.cooldown.mark(winner, turn=self.turn)
        if self.telemetry is not None:
            self.telemetry.record(task.id, result, actual_route=actual_route, operation=self.operation)
        self._last_result = result
        return result

    def feedback(self, verdict: str) -> int:
        """Apply bounded reward only when explicitly enabled; policy edges stay immutable."""
        if not self.learning_enabled or self._last_result is None or not isinstance(self.backend, DeterministicBackend):
            return 0
        rewards = {"PASS": 1.0, "PASS_WITH_RETRY": 0.4, "UNCERTAIN": 0.0, "FAIL": -0.7, "ROLLBACK": -1.0}
        if verdict not in rewards:
            raise ValueError(f"unsupported arbitration feedback: {verdict}")
        winners = set(self._last_result.winners)
        updated = 0
        edges = []
        for edge in self.backend.graph.edges:
            replacement = self.reward_updater.update(edge, rewards[verdict]) if edge.target in winners else edge
            updated += replacement.weight != edge.weight
            edges.append(replacement)
        self.backend.graph.edges = edges
        return updated

    def provider_biases(self, result: ArbitrationResult | None) -> dict[str, float]:
        if result is None or self.operation != "bias":
            return {}
        # Bias is deliberately small and can only rank already-eligible brains.
        return {
            "fast": min(0.15, result.activation("provider.fast") * 0.15),
            "reasoning": min(0.15, result.activation("provider.reasoning") * 0.15),
            "general": 0.0,
            "coder": min(0.15, result.activation("action.implement") * 0.10),
        }
