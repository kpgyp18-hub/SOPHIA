"""Single-active-brain scheduling and observability for constrained local VRAM."""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from sophia.brains.ollama import Brain, BrainRegistry
from sophia.core.health import total_physical_memory_bytes


class ModelLifecycle(StrEnum):
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    UNLOADING = "unloading"
    ERROR = "error"


@dataclass(frozen=True)
class ResourceUsage:
    """Best-effort host measurements; ``None`` means the probe is unavailable."""

    vram_total_mib: int | None = None
    vram_used_mib: int | None = None
    ram_total_bytes: int | None = None

    @property
    def vram_free_mib(self) -> int | None:
        if self.vram_total_mib is None or self.vram_used_mib is None:
            return None
        return max(0, self.vram_total_mib - self.vram_used_mib)


def read_resource_usage() -> ResourceUsage:
    """Read GPU usage without loading a model or requiring an NVIDIA Python SDK."""
    total = used = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        line = next((item.strip() for item in result.stdout.splitlines() if item.strip()), "")
        fields = [item.strip() for item in line.split(",")]
        if len(fields) >= 2:
            total, used = int(fields[0]), int(fields[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return ResourceUsage(total, used, total_physical_memory_bytes())


class ResourceManager:
    """Keep one generation model active and expose scheduling state safely.

    The runtime is sequential today, so a busy inference is never pre-empted.
    Priority is retained with each acquisition request for the v0.7 scheduler and
    is observable now without pretending that concurrent execution is supported.
    """

    def __init__(self, registry: BrainRegistry, *, idle_timeout_seconds: float = 300,
                 inference_timeout_seconds: float = 180, clock: Callable[[], float] = time.monotonic,
                 usage_reader: Callable[[], ResourceUsage] = read_resource_usage) -> None:
        if idle_timeout_seconds < 0 or inference_timeout_seconds <= 0:
            raise ValueError("idle timeout must be non-negative and inference timeout must be positive")
        self.registry = registry
        self.idle_timeout_seconds = idle_timeout_seconds
        self.inference_timeout_seconds = inference_timeout_seconds
        self._clock, self._usage_reader = clock, usage_reader
        self.active_name: str | None = None
        self.active_model: str | None = None
        self._states: dict[str, ModelLifecycle] = {
            self._model_key(name, registry.get(name)): ModelLifecycle.NOT_LOADED
            for name in registry.names()
        }
        self._last_used: dict[str, float] = {}
        self._busy_since: dict[str, float] = {}
        self._priorities: dict[str, int] = {}

    @staticmethod
    def _model_key(name: str, brain: Brain) -> str:
        """Aliases that target one Ollama model share a single loaded resource."""
        return str(getattr(brain, "model", name))

    def can_schedule(self, name: str, *, priority: int = 0) -> bool:
        """Return whether a request can start without interrupting active work."""
        target = self.registry.get(name)
        target_model = self._model_key(name, target)
        if self.active_model is None or self.active_model == target_model:
            return True
        return self._states.get(self.active_model) is not ModelLifecycle.BUSY

    def acquire(self, name: str, *, priority: int = 0) -> Brain:
        target = self.registry.get(name)  # validate the new selection before releasing anything
        if not self.can_schedule(name, priority=priority):
            raise TimeoutError(f"resource busy: cannot pre-empt '{self.active_name}' for '{name}'")
        target_model = self._model_key(name, target)
        self.expire_idle()
        if self.active_name and self.active_model != target_model:
            self._release_active()
        self._states[target_model] = ModelLifecycle.LOADING
        self.active_name = name
        self.active_model = target_model
        try:
            # Most providers load lazily on their first inference.  A provider
            # that exposes an explicit load hook is still checked here so a
            # failed model swap cannot leave a phantom READY resource behind.
            load = getattr(target, "load", None)
            if callable(load):
                load()
        except (ConnectionError, OSError, TimeoutError):
            self._states[target_model] = ModelLifecycle.ERROR
            self.active_name, self.active_model = None, None
            raise
        self._priorities[target_model] = priority
        self._last_used[target_model] = self._clock()
        self._states[target_model] = ModelLifecycle.READY
        return target

    def release(self, name: str | None = None) -> bool:
        """Unload the active model after a task, without releasing another role.

        Roles can be aliases for one provider model, therefore either the active
        role or any role resolving to the active model is accepted.
        """
        if self.active_model is None:
            return False
        if name is not None:
            requested = self._model_key(name, self.registry.get(name))
            if requested != self.active_model:
                return False
        self._release_active()
        return True

    def restore_general(self) -> Brain | None:
        """Return the pool to its configured interactive default after a swap."""
        if not self.registry.has("general"):
            return None
        if self.active_name == "general":
            return self.registry.get("general")
        return self.acquire("general")

    def _release_active(self) -> None:
        assert self.active_name is not None and self.active_model is not None
        previous_model = self.active_model
        previous = self.registry.get(self.active_name)
        self._states[previous_model] = ModelLifecycle.UNLOADING
        unload = getattr(previous, "unload", None)
        if callable(unload):
            try:
                unload()
            except OSError:
                self._states[previous_model] = ModelLifecycle.ERROR
            else:
                self._states[previous_model] = ModelLifecycle.NOT_LOADED
        else:
            self._states[previous_model] = ModelLifecycle.NOT_LOADED
        self.active_name, self.active_model = None, None

    def expire_idle(self) -> bool:
        """Unload a ready model once its configured idle interval has elapsed."""
        model = self.active_model
        if model is None or self._states.get(model) is not ModelLifecycle.READY:
            return False
        if self._clock() - self._last_used.get(model, self._clock()) < self.idle_timeout_seconds:
            return False
        self._release_active()
        return True

    def mark_busy(self, name: str) -> None:
        """Record an active inference without changing the selected model."""
        brain = self.registry.get(name)
        model = self._model_key(name, brain)
        self._states[model] = ModelLifecycle.BUSY
        now = self._clock()
        self._busy_since[model] = now
        self._last_used[model] = now

    def mark_ready(self, name: str) -> None:
        brain = self.registry.get(name)
        model = self._model_key(name, brain)
        self._states[model] = ModelLifecycle.READY
        self._busy_since.pop(model, None)
        self._last_used[model] = self._clock()

    def mark_error(self, name: str) -> None:
        brain = self.registry.get(name)
        model = self._model_key(name, brain)
        self._states[model] = ModelLifecycle.ERROR
        self._busy_since.pop(model, None)

    def check_timeouts(self) -> tuple[str, ...]:
        """Mark overdue inferences failed and return their model identifiers."""
        now = self._clock()
        expired = tuple(model for model, started in self._busy_since.items()
                        if now - started >= self.inference_timeout_seconds)
        for model in expired:
            self._states[model] = ModelLifecycle.ERROR
            self._busy_since.pop(model, None)
        return expired

    def usage(self) -> ResourceUsage:
        return self._usage_reader()

    def status(self) -> dict[str, object]:
        """Structured lifecycle, priority, and host-resource telemetry."""
        usage = self.usage()
        return {
            "active_name": self.active_name,
            "active_model": self.active_model,
            "models": self.snapshot(),
            "priorities": dict(self._priorities),
            "usage": {"vram_total_mib": usage.vram_total_mib, "vram_used_mib": usage.vram_used_mib,
                      "vram_free_mib": usage.vram_free_mib, "ram_total_bytes": usage.ram_total_bytes},
        }

    def snapshot(self) -> dict[str, str]:
        """Return local lifecycle state by model identifier for health telemetry."""
        self.check_timeouts()
        return {model: state.value for model, state in self._states.items()}
