"""Deterministic context selection with hard trust and budget boundaries."""
from __future__ import annotations

from dataclasses import asdict
import json

from sophia.context.models import ContextPack, ContextRequest, ContextTrust, ResolvedContext
from sophia.context.registry import ContextRegistry
from sophia.runtime.budget import ContextBudget


class ContextConflictError(ValueError):
    pass


_ALLOWED_TRUST = frozenset({
    ContextTrust.SYSTEM, ContextTrust.INTERNAL,
    ContextTrust.PROJECT_APPROVED, ContextTrust.TRUSTED_EXTERNAL,
})


class ContextResolver:
    def __init__(self, registry: ContextRegistry) -> None:
        self.registry = registry
        self._cache: dict[str, ResolvedContext] = {}

    def resolve(self, request: ContextRequest) -> ResolvedContext:
        if request.token_budget <= 0:
            raise ValueError("context token_budget must be positive")
        key = json.dumps({"request": asdict(request), "registry": self.registry.fingerprint()}, sort_keys=True)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        explicit = set(request.explicit_contexts)
        missing = sorted(context_id for context_id in explicit if self.registry.get(context_id) is None)
        if missing:
            raise KeyError(f"unknown or inactive contexts: {', '.join(missing)}")
        candidates: list[tuple[int, ContextPack, tuple[str, ...]]] = []
        skipped: dict[str, str] = {}
        terms = {term for term in f"{request.task_type} {request.query}".casefold().split() if term}
        for pack in self.registry.list():
            reason: list[str] = []
            if pack.trust not in _ALLOWED_TRUST:
                skipped[pack.context_id] = "untrusted"
                continue
            if pack.is_judge != request.judge:
                if pack.context_id in explicit:
                    raise ValueError(f"explicit context crosses judge/generation boundary: {pack.context_id}")
                skipped[pack.context_id] = "judge/generation boundary"
                continue
            if pack.artifact_types and request.artifact_type not in pack.artifact_types:
                if pack.context_id in explicit:
                    raise ValueError(f"explicit context has artifact mismatch: {pack.context_id}")
                skipped[pack.context_id] = "artifact mismatch"
                continue
            if pack.skills and request.skill_id not in pack.skills:
                if pack.context_id in explicit:
                    raise ValueError(f"explicit context has skill mismatch: {pack.context_id}")
                skipped[pack.context_id] = "skill mismatch"
                continue
            if pack.projects and request.project_id not in pack.projects:
                if pack.context_id in explicit:
                    raise ValueError(f"explicit context has project mismatch: {pack.context_id}")
                skipped[pack.context_id] = "project mismatch"
                continue
            score = pack.priority
            if pack.context_id in explicit:
                score += 1_000
                reason.append("explicit")
            if pack.scope == "global":
                score += 400
                reason.append("global")
            if pack.artifact_types:
                score += 100
                reason.append("artifact")
            if pack.skills:
                score += 100
                reason.append("skill")
            if pack.projects:
                score += 120
                reason.append("project")
            matched = sorted(
                trigger for trigger in pack.triggers
                if trigger.casefold() in terms or trigger.casefold() in request.query.casefold()
            )
            if matched:
                score += len(matched) * 20
                reason.append("trigger:" + ",".join(matched))
            if not reason and pack.context_id not in explicit:
                skipped[pack.context_id] = "not relevant"
                continue
            candidates.append((score, pack, tuple(reason)))

        selected: list[ContextPack] = []
        reasons: dict[str, tuple[str, ...]] = {}
        conflicts: list[str] = []
        used = 0
        for _score, pack, reason in sorted(candidates, key=lambda item: (-item[0], item[1].context_id)):
            conflict = next(
                (other for other in selected if other.context_id in pack.conflicts_with or pack.context_id in other.conflicts_with),
                None,
            )
            if conflict is not None:
                detail = f"{pack.context_id} conflicts with {conflict.context_id}"
                if pack.context_id in explicit and conflict.context_id in explicit:
                    raise ContextConflictError(detail)
                conflicts.append(detail)
                skipped[pack.context_id] = "conflict"
                continue
            cost = min(ContextBudget.estimate(pack.content), pack.max_tokens)
            if used + cost > request.token_budget:
                if pack.context_id in explicit:
                    raise ValueError(f"explicit context exceeds token budget: {pack.context_id}")
                skipped[pack.context_id] = "token budget"
                continue
            selected.append(pack)
            reasons[pack.context_id] = reason
            used += cost
        selected_ids = {pack.context_id for pack in selected}
        missing_requirements = sorted({required for pack in selected for required in pack.requires if required not in selected_ids})
        if missing_requirements:
            raise ValueError(f"unresolved context requirements: {', '.join(missing_requirements)}")
        result = ResolvedContext(request, tuple(selected), reasons, used, tuple(conflicts), skipped)
        self._cache[key] = result
        return result

    def clear_cache(self) -> None:
        self._cache.clear()
