"""Configuration-driven local Brain Pool."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sophia.brains.cli import CliBrain
from sophia.brains.cli_transport import CliTransport
from sophia.brains.ollama import BrainRegistry, OllamaBrain, OllamaEmbedder
from sophia.arbitration import ArbitrationMode, ArbitrationTelemetry, NeuralArbitrator, RewardUpdater


def load_registry(config_path: str | Path = "config/system.yaml", *,
                  current_persona: str | None = None) -> BrainRegistry:
    config_file = Path(config_path)
    data: dict[str, Any] = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    endpoint = str(data.get("ollama_endpoint", "http://127.0.0.1:11434"))
    definitions = data.get("brains", {})
    if not isinstance(definitions, dict):
        raise ValueError("brains must be a mapping")
    registry = BrainRegistry()
    for name, definition in definitions.items():
        if not isinstance(definition, dict) or definition.get("enabled", True) is False:
            continue
        provider = definition.get("provider")
        if provider == "ollama":
            if not definition.get("model"):
                raise ValueError(f"brain '{name}' requires an enabled Ollama model")
            think = definition.get("think")
            if think is not None and not isinstance(think, bool):
                raise ValueError(f"brain '{name}' think must be a boolean when configured")
            registry.register(str(name), OllamaBrain(str(definition["model"]), endpoint, think=think))
        elif provider in {"claude-cli", "codex-cli"}:
            persona = str(provider).removesuffix("-cli")
            registry.register(str(name), CliBrain(
                persona, CliTransport(persona), current_persona=current_persona,
            ))
        else:
            raise ValueError(f"brain '{name}' has unsupported provider: {provider}")
    for required in ("general", "coder"):
        if not registry.has(required):
            raise ValueError(f"required brain missing: {required}")
    return registry


def load_database_config(config_path: str | Path = "config/system.yaml") -> dict[str, Any]:
    """Read database settings without coupling brain construction to storage."""
    data: dict[str, Any] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    database = data.get("database", {})
    if not isinstance(database, dict):
        raise ValueError("database must be a mapping")
    return database


def load_resource_manager_config(config_path: str | Path = "config/system.yaml") -> dict[str, float]:
    """Read the small, validated subset consumed by ``ResourceManager``."""
    data: dict[str, Any] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    settings = data.get("resource_manager", {})
    if not isinstance(settings, dict):
        raise ValueError("resource_manager must be a mapping")
    result: dict[str, float] = {}
    for key in ("idle_timeout_seconds", "inference_timeout_seconds"):
        if key in settings:
            value = settings[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"resource_manager.{key} must be numeric")
            result[key] = float(value)
    return result


def load_embedder(config_path: str | Path = "config/system.yaml") -> OllamaEmbedder:
    """Build the configured local embedding client with its schema dimension."""
    data: dict[str, Any] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    embedding = data.get("embedding", {})
    if not isinstance(embedding, dict) or embedding.get("provider") != "ollama" or not isinstance(embedding.get("model"), str):
        raise ValueError("embedding requires an Ollama provider and model")
    dimensions = embedding.get("dimensions")
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0:
        raise ValueError("embedding.dimensions must be a positive integer")
    return OllamaEmbedder(embedding["model"], str(data.get("ollama_endpoint", "http://127.0.0.1:11434")), dimensions=dimensions)


def load_arbitrator(config_path: str | Path = "config/system.yaml") -> NeuralArbitrator:
    data: dict[str, Any] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    settings = data.get("arbitration", {})
    if not isinstance(settings, dict):
        raise ValueError("arbitration must be a mapping")
    backend = settings.get("backend", "deterministic")
    if backend != "deterministic":
        raise ValueError("only the deterministic arbitration backend is supported")
    enabled = settings.get("enabled", False)
    learning = settings.get("learning", {})
    if not isinstance(enabled, bool) or not isinstance(learning, dict):
        raise ValueError("arbitration enabled/learning configuration is invalid")
    learning_enabled = learning.get("enabled", False)
    if not isinstance(learning_enabled, bool):
        raise ValueError("arbitration.learning.enabled must be boolean")
    rate = learning.get("rate", 0.02)
    minimum_samples = learning.get("minimum_samples", 3)
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not isinstance(minimum_samples, int):
        raise ValueError("arbitration learning rate/minimum_samples are invalid")
    operation = str(settings.get("operation", "shadow"))
    mode_settings = settings.get("modes", {})
    if not isinstance(mode_settings, dict):
        raise ValueError("arbitration.modes must be a mapping")
    mode = ArbitrationMode(str(mode_settings.get("default", "normal")))
    telemetry = ArbitrationTelemetry(str(settings.get("telemetry_path", "workspace/sophia/arbitration.jsonl")))
    return NeuralArbitrator.default(
        enabled=enabled, operation=operation, mode=mode, telemetry=telemetry,
        learning_enabled=learning_enabled,
        reward_updater=RewardUpdater(learning_rate=float(rate), minimum_samples=minimum_samples),
    )
