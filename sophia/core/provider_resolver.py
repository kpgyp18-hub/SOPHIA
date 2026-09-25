"""Deterministic, capability-aware provider selection.

The resolver is policy only: it chooses a registered local brain but never
loads a model or invokes a provider.  Keeping eligibility here makes every
runtime path use the same recursion, health, privacy, and resource filters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from sophia.brains.ollama import BrainRegistry
from sophia.core.escalation import EscalationRoute
from sophia.runtime.task import Task


@dataclass(frozen=True)
class ProviderSelection:
    brain: str
    provider: str
    reason: str
    requires_user_approval: bool = False
    criteria: dict[str, object] | None = None
    excluded: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EligibleProvider:
    brain: str
    provider: str
    static_rank: int
    adaptive_score: float | None = None


class ProviderResolver:
    """Resolve a healthy, non-recursive local brain for a capability request."""

    # The first item is always the requested capability.  Later items are safe
    # local fallbacks, not permission to change the requested task's policy.
    _CAPABILITY_CANDIDATES = {
        "fast": ("fast", "general"),
        "general": ("general", "fast"),
        "reasoning": ("reasoning", "general"),
        "coder": ("coder", "general"),
        "vision": ("vision", "general"),
    }

    def __init__(self, registry: BrainRegistry) -> None:
        self.registry = registry

    def resolve(self, task: Task, requested_brain: str, *, route: EscalationRoute,
                health: dict[str, bool] | None = None, privacy_required: bool = True,
                visited_providers: frozenset[str] = frozenset(), context_tokens: int = 0,
                resource_available: Callable[[str], bool] | None = None,
                adaptive_scores: Mapping[str, float] | None = None) -> ProviderSelection:
        """Return the highest-ranked eligible local provider.

        ``resource_available`` is deliberately a read-only callback.  The
        ResourceManager remains the only component that can acquire or unload
        a model after this selection has been made.
        """
        criteria = {
            "task_type": task.task_type.value, "complexity": task.complexity,
            "health": dict(health or {}), "privacy_required": privacy_required,
            "visited_providers": sorted(visited_providers), "context_tokens": context_tokens,
            "required_capability": requested_brain,
        }
        if route is EscalationRoute.USER_APPROVAL:
            return ProviderSelection(requested_brain, "external", "external provider requires user approval", True, criteria)
        candidates = self._candidate_names(requested_brain)
        criteria["candidate_brains"] = list(candidates)
        eligible, excluded = self._eligible_candidates(
            candidates, health=health, privacy_required=privacy_required,
            visited_providers=visited_providers, resource_available=resource_available,
            adaptive_scores=adaptive_scores,
        )
        if eligible:
            has_scores = any(item.adaptive_score is not None for item in eligible)
            ranked = sorted(
                eligible,
                key=(lambda item: (-(item.adaptive_score or 0.0), item.static_rank))
                if has_scores else (lambda item: item.static_rank),
            )
            selected = ranked[0]
            criteria["eligible_brains"] = [item.brain for item in eligible]
            criteria["adaptive_scores"] = {
                item.brain: item.adaptive_score for item in eligible if item.adaptive_score is not None
            }
            reason = (
                "adaptive eligible provider"
                if has_scores else
                ("configured capability provider" if selected.brain == requested_brain
                 else "eligible local capability fallback")
            )
            return ProviderSelection(selected.brain, selected.provider, reason, False, criteria, excluded)
        criteria["excluded"] = dict(excluded)
        raise LookupError(f"no eligible provider for task {task.id} capability '{requested_brain}': {excluded}")

    def _eligible_candidates(self, candidates: tuple[str, ...], *,
                             health: dict[str, bool] | None,
                             privacy_required: bool,
                             visited_providers: frozenset[str],
                             resource_available: Callable[[str], bool] | None,
                             adaptive_scores: Mapping[str, float] | None = None,
                             ) -> tuple[list[EligibleProvider], dict[str, str]]:
        """Evaluate every candidate before ranking; hard exclusions are final."""
        eligible: list[EligibleProvider] = []
        excluded: dict[str, str] = {}
        for rank, name in enumerate(candidates):
            if not self.registry.has(name):
                excluded[name] = "unavailable"
                continue
            brain = self.registry.get(name)
            provider = str(getattr(brain, "provider", "ollama"))
            if provider in visited_providers:
                excluded[name] = "provider already active for this task"
                continue
            if health and not health.get(provider, True):
                excluded[name] = "provider unhealthy"
                continue
            if privacy_required and bool(getattr(brain, "external", False)):
                excluded[name] = "privacy blocks external provider"
                continue
            if resource_available is not None and not resource_available(name):
                excluded[name] = "insufficient or busy resource"
                continue
            score = None if adaptive_scores is None or name not in adaptive_scores else float(adaptive_scores[name])
            eligible.append(EligibleProvider(name, provider, rank, score))
        return eligible, excluded

    def _candidate_names(self, requested_brain: str) -> tuple[str, ...]:
        names = self._CAPABILITY_CANDIDATES.get(requested_brain, (requested_brain, "general"))
        # Preserve ranking while avoiding a duplicate for ``general`` and for
        # aliases a future registry may add.
        return tuple(dict.fromkeys(names))
