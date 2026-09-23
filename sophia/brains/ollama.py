from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class BrainResult:
    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_ns: int = 0
    estimated_cost: float = 0.0
    model: str | None = None
    provider: str | None = None


class Brain(Protocol):
    """Minimal interchangeable brain contract."""

    def generate(self, prompt: str) -> BrainResult: ...


class OllamaBrain:
    def __init__(self, model: str, endpoint: str = "http://127.0.0.1:11434",
                 *, think: bool | None = None) -> None:
        self.model, self.endpoint, self.think = model, endpoint.rstrip("/"), think

    def generate(self, prompt: str) -> BrainResult:
        payload: dict[str, object] = {"model": self.model, "prompt": prompt, "stream": False}
        if self.think is not None:
            payload["think"] = self.think
        body = json.dumps(payload).encode()
        request = Request(f"{self.endpoint}/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=180) as response:
            data = json.load(response)
        return BrainResult(data["response"].strip(), data.get("prompt_eval_count", 0),
                           data.get("eval_count", 0), data.get("total_duration", 0),
                           model=data.get("model", self.model), provider="ollama")

    def load(self) -> None:
        """Warm this local model without treating it as an inference result.

        Ollama keeps the requested model resident for the normal resource-manager
        idle interval.  This makes a ResourceManager swap an actual provider
        lifecycle operation rather than merely an in-memory state transition.
        """
        body = json.dumps({"model": self.model, "prompt": "", "stream": False, "keep_alive": "5m"}).encode()
        request = Request(f"{self.endpoint}/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=180):
            pass

    def unload(self) -> None:
        """Ask Ollama to release this model when another brain becomes active."""
        body = json.dumps({"model": self.model, "keep_alive": 0}).encode()
        request = Request(f"{self.endpoint}/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30):
            pass


class OllamaEmbedder:
    """Bounded local embedding client; it never exposes a generation tool."""

    def __init__(self, model: str, endpoint: str = "http://127.0.0.1:11434", *, dimensions: int | None = None) -> None:
        self.model, self.endpoint, self.dimensions = model, endpoint.rstrip("/"), dimensions

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"model": self.model, "input": text, "keep_alive": 0}).encode()
        request = Request(f"{self.endpoint}/api/embed", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=90) as response:
            data = json.load(response)
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != 1 or not isinstance(embeddings[0], list):
            raise RuntimeError("Ollama returned an invalid embedding response")
        vector = [float(value) for value in embeddings[0]]
        if self.dimensions is not None and len(vector) != self.dimensions:
            raise RuntimeError(f"embedding dimension mismatch: expected {self.dimensions}, received {len(vector)}")
        return vector


class BrainRegistry:
    def __init__(self) -> None:
        self._brains: dict[str, Brain] = {}

    def register(self, name: str, brain: Brain) -> None:
        self._brains[name] = brain

    def get(self, name: str) -> Brain:
        return self._brains[name]

    def has(self, name: str) -> bool:
        return name in self._brains

    def names(self) -> tuple[str, ...]:
        """Expose configured brain names without leaking the registry mapping."""
        return tuple(self._brains)
