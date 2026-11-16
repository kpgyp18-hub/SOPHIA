"""Compose skill, context, template, judge, and lineage into one plan."""
from __future__ import annotations

from dataclasses import dataclass

from sophia.artifact_intelligence.template_resolver import TemplateRequest, TemplateResolution, TemplateResolver
from sophia.context import ContextBuilder, ContextRequest, ContextResolver, context_provenance


@dataclass(frozen=True)
class ArtifactPlanRequest:
    task_type: str
    artifact_type: str
    skill_id: str
    query: str = ""
    project_id: str | None = None
    workflow_id: str | None = None
    contexts: tuple[str, ...] = ()
    judge_contexts: tuple[str, ...] = ()
    token_budget: int = 4_000
    business_type: str | None = None
    organization: str | None = None
    locale: str | None = None
    requested_style: str | None = None
    template_id: str | None = None


@dataclass(frozen=True)
class ArtifactPlan:
    request: ArtifactPlanRequest
    generation_context: str
    generation_context_ids: tuple[str, ...]
    judge_context: str
    judge_context_ids: tuple[str, ...]
    template: TemplateResolution
    provenance: tuple[dict[str, object], ...]


class ArtifactPlanner:
    def __init__(self, contexts: ContextResolver, templates: TemplateResolver | None = None) -> None:
        self.contexts = contexts
        self.templates = templates or TemplateResolver()
        self.builder = ContextBuilder()

    def plan(self, request: ArtifactPlanRequest) -> ArtifactPlan:
        base = dict(
            task_type=request.task_type, artifact_type=request.artifact_type,
            skill_id=request.skill_id, query=request.query, project_id=request.project_id,
            workflow_id=request.workflow_id, token_budget=request.token_budget,
        )
        generation = self.contexts.resolve(ContextRequest(
            **base, explicit_contexts=request.contexts, judge=False,
        ))
        judge = self.contexts.resolve(ContextRequest(
            **base, explicit_contexts=request.judge_contexts, judge=True,
        ))
        generation_ids = tuple(pack.context_id for pack in generation.selected)
        judge_ids = tuple(pack.context_id for pack in judge.selected)
        overlap = set(generation_ids) & set(judge_ids)
        if overlap:
            raise ValueError(f"generation/judge context overlap: {', '.join(sorted(overlap))}")
        template = self.templates.resolve(TemplateRequest(
            artifact_type=request.artifact_type, business_type=request.business_type,
            organization=request.organization, project_id=request.project_id,
            locale=request.locale, requested_style=request.requested_style,
            explicit_template=request.template_id,
        ))
        provenance = tuple(
            entry.to_record() for entry in (*context_provenance(generation), *context_provenance(judge))
        )
        if template.template is not None:
            provenance += ({
                "template_id": template.template.template_id,
                "template_version": template.template.version,
                "selection_reason": template.reason,
            },)
        return ArtifactPlan(
            request, self.builder.build(generation), generation_ids,
            self.builder.build(judge), judge_ids, template, provenance,
        )

