"""Role-oriented model inventory used by the v0.7 runtime."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class ModelRole(StrEnum):
    GENERAL = "general"
    CODER = "coder"
    FAST = "fast"
    REASONING = "reasoning"
    VISION = "vision"
    EMBEDDING = "embedding"
    SAFETY_FAST = "safety_fast"
    SAFETY_DEEP = "safety_deep"


@dataclass(frozen=True)
class ModelSpec:
    role: ModelRole
    provider: str
    model: str
    shared_with: ModelRole | None = None


class ModelRegistry:
    """Validated inventory separate from live loading and scheduling decisions."""

    def __init__(self, specs: tuple[ModelSpec, ...]) -> None:
        self._specs = {spec.role: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("model roles must be unique")
        for role in (ModelRole.GENERAL, ModelRole.CODER, ModelRole.FAST):
            if role not in self._specs:
                raise ValueError(f"required model role missing: {role.value}")

    @classmethod
    def from_config(cls, config_path: str | Path = "config/system.yaml") -> "ModelRegistry":
        data: dict[str, Any] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        brains = data.get("brains", {})
        if not isinstance(brains, dict):
            raise ValueError("brains must be a mapping")
        specs: list[ModelSpec] = []
        for role in (ModelRole.GENERAL, ModelRole.CODER, ModelRole.FAST, ModelRole.REASONING,
                     ModelRole.VISION, ModelRole.SAFETY_FAST, ModelRole.SAFETY_DEEP):
            item = brains.get(role.value)
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            provider, model = item.get("provider"), item.get("model")
            if not isinstance(provider, str) or not isinstance(model, str):
                raise ValueError(f"brain '{role.value}' requires provider and model")
            shared_with = ModelRole.GENERAL if role is ModelRole.VISION and model == brains.get("general", {}).get("model") else None
            specs.append(ModelSpec(role, provider, model, shared_with))
        embedding = data.get("embedding", {})
        if isinstance(embedding, dict) and isinstance(embedding.get("provider"), str) and isinstance(embedding.get("model"), str):
            specs.append(ModelSpec(ModelRole.EMBEDDING, embedding["provider"], embedding["model"]))
        return cls(tuple(specs))

    def get(self, role: ModelRole | str) -> ModelSpec:
        return self._specs[ModelRole(role)]

    def roles(self) -> tuple[ModelRole, ...]:
        return tuple(self._specs)

    def specs(self) -> tuple[ModelSpec, ...]:
        return tuple(self._specs.values())
