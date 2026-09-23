import pytest

from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.escalation import EscalationRoute
from sophia.core.provider_resolver import ProviderResolver
from sophia.runtime.task import Task


def _resolver(*names: str) -> ProviderResolver:
    registry = BrainRegistry()
    for name in names:
        brain = DummyBrain(name)
        brain.provider = name
        registry.register(name, brain)
    return ProviderResolver(registry)


def test_provider_resolver_capability_falls_back_to_eligible_local_brain():
    selection = _resolver("general").resolve(
        Task("reason about this"), "reasoning", route=EscalationRoute.LOCAL,
    )
    assert selection.brain == "general"
    assert selection.excluded == {"reasoning": "unavailable"}


def test_provider_resolver_health_filter_excludes_unhealthy_requested_provider():
    selection = _resolver("reasoning", "general").resolve(
        Task("reason about this"), "reasoning", route=EscalationRoute.LOCAL,
        health={"reasoning": False, "general": True},
    )
    assert selection.brain == "general"
    assert selection.excluded["reasoning"] == "provider unhealthy"


def test_provider_resolver_excludes_visited_provider_without_falling_back_to_it():
    resolver = _resolver("reasoning", "general")
    with pytest.raises(LookupError, match="no eligible provider"):
        resolver.resolve(
            Task("reason about this"), "reasoning", route=EscalationRoute.LOCAL,
            visited_providers=frozenset({"reasoning", "general"}),
        )


def test_provider_resolver_recursion_exclusion_applies_to_shared_provider_aliases():
    registry = BrainRegistry()
    for name in ("reasoning", "general"):
        brain = DummyBrain(name)
        brain.provider = "ollama"
        registry.register(name, brain)
    with pytest.raises(LookupError, match="no eligible provider"):
        ProviderResolver(registry).resolve(
            Task("reason about this"), "reasoning", route=EscalationRoute.LOCAL,
            visited_providers=frozenset({"ollama"}),
        )


def test_provider_resolver_honors_read_only_resource_filter():
    selection = _resolver("reasoning", "general").resolve(
        Task("reason about this"), "reasoning", route=EscalationRoute.LOCAL,
        resource_available=lambda name: name == "general",
    )
    assert selection.brain == "general"
    assert selection.excluded["reasoning"] == "insufficient or busy resource"
