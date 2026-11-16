"""Host-owned template selection; templates never grant execution authority."""
from __future__ import annotations

from dataclasses import dataclass, field

from sophia.context.validator import semantic_version


@dataclass(frozen=True)
class TemplateDefinition:
    template_id: str
    version: str
    artifact_type: str
    business_types: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    locales: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    mapping: dict[str, str] = field(default_factory=dict)
    priority: int = 0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.template_id.startswith("template."):
            raise ValueError("template_id must start with template.")
        semantic_version(self.version)


@dataclass(frozen=True)
class TemplateRequest:
    artifact_type: str
    business_type: str | None = None
    organization: str | None = None
    project_id: str | None = None
    locale: str | None = None
    requested_style: str | None = None
    explicit_template: str | None = None


@dataclass(frozen=True)
class TemplateResolution:
    template: TemplateDefinition | None
    reason: str
    fallback: bool = False


class TemplateResolver:
    def __init__(self) -> None:
        self._templates: dict[str, TemplateDefinition] = {}

    def register(self, definition: TemplateDefinition) -> TemplateDefinition:
        current = self._templates.get(definition.template_id)
        if current is not None and semantic_version(definition.version) < semantic_version(current.version):
            raise ValueError(f"template downgrade rejected: {current.version} -> {definition.version}")
        if current is not None and definition.version == current.version and definition != current:
            raise ValueError("same-version template conflict")
        self._templates[definition.template_id] = definition
        return definition

    def resolve(self, request: TemplateRequest) -> TemplateResolution:
        if request.explicit_template:
            template = self._templates.get(request.explicit_template)
            if template is None or not template.enabled:
                raise KeyError(request.explicit_template)
            if template.artifact_type != request.artifact_type:
                raise ValueError("explicit template artifact mismatch")
            return TemplateResolution(template, "explicit")
        ranked: list[tuple[int, TemplateDefinition]] = []
        for template in self._templates.values():
            if not template.enabled or template.artifact_type != request.artifact_type:
                continue
            score = template.priority
            compatible = True
            for allowed, value, weight in (
                (template.business_types, request.business_type, 20),
                (template.organizations, request.organization, 30),
                (template.projects, request.project_id, 40),
                (template.locales, request.locale, 10),
                (template.styles, request.requested_style, 10),
            ):
                if allowed and value not in allowed:
                    compatible = False
                    break
                if allowed:
                    score += weight
            if compatible:
                ranked.append((score, template))
        if not ranked:
            return TemplateResolution(None, "no compatible template", fallback=True)
        selected = sorted(ranked, key=lambda item: (-item[0], item[1].template_id))[0][1]
        return TemplateResolution(selected, "best compatible template")

