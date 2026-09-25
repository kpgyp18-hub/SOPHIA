"""Bounded adaptive routing and benchmark evidence for v0.8."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sophia.core.escalation import EscalationRoute
from sophia.core.provider_resolver import ProviderResolver, ProviderSelection
from sophia.core.resource_manager import ResourceManager
from sophia.runtime.task import Task


@dataclass(frozen=True)
class BenchmarkResult:
    brain: str
    quality: float
    latency_ms: float
    reliable: bool
    resource_score: float = 1.0

    @property
    def adaptive_score(self) -> float:
        latency_score = 1.0 / (1.0 + max(0.0, self.latency_ms) / 1_000.0)
        return max(0.0, min(1.0, 0.50 * self.quality + 0.20 * latency_score
                            + 0.20 * float(self.reliable) + 0.10 * self.resource_score))


class AdaptiveScoreStore:
    def __init__(self) -> None:
        self._scores: dict[str, float] = {}

    def record(self, result: BenchmarkResult) -> float:
        self._scores[result.brain] = result.adaptive_score
        return result.adaptive_score

    def scores(self) -> dict[str, float]:
        return dict(self._scores)


class BrainBenchmark:
    """Runs only a provider that passed resolver eligibility, then acquires it."""

    def __init__(self, resolver: ProviderResolver, resources: ResourceManager,
                 scores: AdaptiveScoreStore, *, telemetry_path: str | Path | None = None) -> None:
        self.resolver, self.resources, self.scores = resolver, resources, scores
        self.telemetry_path = Path(telemetry_path) if telemetry_path else None

    def run(self, task: Task, requested_brain: str, evaluator: Callable[[object], BenchmarkResult], *,
            health: dict[str, bool] | None = None) -> tuple[ProviderSelection, BenchmarkResult]:
        selection = self.resolver.resolve(
            task, requested_brain, route=EscalationRoute.LOCAL, health=health,
            privacy_required=True, resource_available=self.resources.can_schedule,
        )
        brain = self.resources.acquire(selection.brain)
        try:
            result = evaluator(brain)
            if result.brain != selection.brain:
                raise ValueError("benchmark result brain does not match eligible selection")
            self.scores.record(result)
            self._record(result)
            return selection, result
        finally:
            self.resources.release(selection.brain)

    def _record(self, result: BenchmarkResult) -> None:
        if self.telemetry_path is None:
            return
        self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**asdict(result), "adaptive_score": result.adaptive_score,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
        with self.telemetry_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class FailureRecord:
    task_type: str
    skill_id: str | None
    brain: str
    reason: str
    timestamp: str


class FailureMemory:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._records: list[FailureRecord] = []

    def record(self, *, task_type: str, brain: str, reason: str, skill_id: str | None = None) -> FailureRecord:
        record = FailureRecord(task_type, skill_id, brain, reason, datetime.now(timezone.utc).isoformat())
        self._records.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        return record

    def count(self, *, task_type: str, brain: str | None = None, skill_id: str | None = None,
              reason: str | None = None) -> int:
        return sum(
            record.task_type == task_type
            and (brain is None or record.brain == brain)
            and (skill_id is None or record.skill_id == skill_id)
            and (reason is None or record.reason == reason)
            for record in self._records
        )
