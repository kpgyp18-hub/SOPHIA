"""Deterministic local brain for integration tests and offline demos."""
from __future__ import annotations

from sophia.brains.ollama import BrainResult


class DummyBrain:
    def __init__(self, response: str = "dummy response") -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> BrainResult:
        self.prompts.append(prompt)
        return BrainResult(self.response, prompt_tokens=max(1, len(prompt.split())), output_tokens=2,
                           duration_ns=1, estimated_cost=0.0, model="dummy", provider="dummy")
