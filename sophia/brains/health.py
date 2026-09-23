"""Ollama model-inventory checks that do not load models into VRAM."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable
from urllib.request import urlopen

from sophia.brains.model_registry import ModelRegistry


@dataclass(frozen=True)
class ModelHealth:
    role: str
    model: str
    installed: bool
    detail: str


class ModelHealthChecker:
    """Validate registered Ollama models by inventory before an inference smoke test."""

    def __init__(self, models: ModelRegistry, endpoint: str = "http://127.0.0.1:11434",
                 opener: Callable[..., object] = urlopen) -> None:
        self.models, self.endpoint, self._opener = models, endpoint.rstrip("/"), opener

    def check(self) -> tuple[ModelHealth, ...]:
        try:
            with self._opener(f"{self.endpoint}/api/tags", timeout=3) as response:
                payload = json.load(response)
            installed = {str(item.get("name")) for item in payload.get("models", []) if isinstance(item, dict)}
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return tuple(ModelHealth(role.value, spec.model, False, f"Ollama unavailable: {error}")
                         for role, spec in ((spec.role, spec) for spec in self.models.specs()))
        return tuple(ModelHealth(spec.role.value, spec.model, spec.model in installed,
                                 "installed" if spec.model in installed else "not installed")
                     for spec in self.models.specs())
