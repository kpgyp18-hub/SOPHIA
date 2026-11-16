"""Small integration facade for the v0.10.1 planning layer."""
from __future__ import annotations

from pathlib import Path

from sophia.artifact_intelligence.planner import ArtifactPlan, ArtifactPlanRequest, ArtifactPlanner
from sophia.artifact_intelligence.quality_profile import QualityProfile, QualityProfileLoader
from sophia.artifact_intelligence.template_resolver import TemplateResolver
from sophia.artifact_intelligence.workflow import WorkflowDefinition, WorkflowLoader
from sophia.context import ContextLoader, ContextRegistry, ContextResolver
from sophia.database import DatabaseManager


class ArtifactIntelligenceManager:
    def __init__(self, database: DatabaseManager, *, context_root: str | Path | None = None,
                 quality_root: str | Path | None = None,
                 workflow_root: str | Path | None = None) -> None:
        package_root = Path(__file__).resolve().parents[1]
        self.context_loader = ContextLoader(context_root or package_root / "contexts")
        self.context_registry = ContextRegistry(database)
        self.context_resolver = ContextResolver(self.context_registry)
        self.template_resolver = TemplateResolver()
        self.planner = ArtifactPlanner(self.context_resolver, self.template_resolver)
        self.quality_loader = QualityProfileLoader(quality_root or package_root / "quality_profiles")
        self.quality_profiles: dict[str, QualityProfile] = {}
        self.workflow_loader = WorkflowLoader(workflow_root or package_root / "workflows")
        self.workflows: dict[str, WorkflowDefinition] = {}

    def sync(self) -> tuple[int, int]:
        contexts = self.context_registry.sync(self.context_loader)
        self.quality_profiles = self.quality_loader.load()
        self.workflows = self.workflow_loader.load()
        self.context_resolver.clear_cache()
        return len(contexts), len(self.quality_profiles)

    def plan(self, request: ArtifactPlanRequest) -> ArtifactPlan:
        return self.planner.plan(request)
