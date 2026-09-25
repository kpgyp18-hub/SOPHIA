from __future__ import annotations

from collections import Counter

from sophia.arbitration.edge import Edge


class RewardUpdater:
    PROTECTED_PREFIXES = ("safety.", "permission.", "secret.", "persona.", "recursion.", "circuit.")

    def __init__(self, *, learning_rate: float = 0.02, minimum_samples: int = 3,
                 min_weight: float = -1.0, max_weight: float = 1.0) -> None:
        if not 0 < learning_rate <= 0.1 or minimum_samples < 1 or min_weight >= max_weight:
            raise ValueError("invalid bounded reward updater configuration")
        self.learning_rate, self.minimum_samples = learning_rate, minimum_samples
        self.min_weight, self.max_weight = min_weight, max_weight
        self._samples: Counter[tuple[str, str]] = Counter()

    def update(self, edge: Edge, reward: float) -> Edge:
        key = (edge.source, edge.target)
        self._samples[key] += 1
        if (not edge.learnable or edge.target.startswith(self.PROTECTED_PREFIXES)
                or self._samples[key] < self.minimum_samples):
            return edge
        weight = max(self.min_weight, min(self.max_weight, edge.weight + self.learning_rate * reward))
        return Edge(edge.source, edge.target, weight, edge.kind, edge.learnable)

    @staticmethod
    def decay(edge: Edge, factor: float) -> Edge:
        if not 0.0 <= factor <= 1.0:
            raise ValueError("decay factor must be between zero and one")
        return Edge(edge.source, edge.target, edge.weight * factor, edge.kind, edge.learnable)
