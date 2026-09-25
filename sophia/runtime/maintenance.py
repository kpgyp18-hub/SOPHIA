"""R0/R1-only housekeeping coordinator.

The coordinator can update runtime-owned metrics and produce approval
candidates. It intentionally has no ToolRegistry dispatch or activation API.
"""
from __future__ import annotations

from dataclasses import dataclass

from sophia.core.adaptive import BrainBenchmark
from sophia.runtime.memory import ProjectMemory
from sophia.skills.registry import SkillRegistry


@dataclass(frozen=True)
class MaintenanceReport:
    consolidated_memories: int
    low_quality_skill_candidates: tuple[str, ...]
    benchmark_requested: bool
    maximum_risk: str = "R1"


class AutonomousMaintenance:
    def __init__(self, memory: ProjectMemory, skills: SkillRegistry,
                 benchmark: BrainBenchmark | None = None) -> None:
        self.memory, self.skills, self.benchmark = memory, skills, benchmark

    def run_local(self, *, quality_threshold: float = 0.5, minimum_samples: int = 3) -> MaintenanceReport:
        consolidated = self.memory.consolidate()
        candidates = tuple(
            skill.skill_id for skill in self.skills.list(status=None)
            if skill.quality_samples >= minimum_samples and skill.quality_score < quality_threshold
        )
        return MaintenanceReport(consolidated, candidates, self.benchmark is not None)
