"""External CLI brains with an explicit self-reference safety boundary."""
from __future__ import annotations

from collections.abc import Callable

from sophia.brains.ollama import BrainResult


class SelfReferenceError(RuntimeError):
    """Raised before a supervisor can invoke the CLI that hosts it."""


class CliBrain:
    """Provider adapter; process ownership remains with the integration layer.

    A transport is injected so the core has no hidden subprocess path.  The
    orchestrator's existing bounded retry policy applies to transport failures.
    """
    def __init__(self, target_persona: str, transport: Callable[[str], BrainResult] | None = None,
                 *, current_persona: str | None = None) -> None:
        self.target_persona = target_persona.lower()
        self.current_persona = current_persona.lower() if current_persona else None
        self.transport = transport
        self.model = f"{self.target_persona}-cli"
        self.provider = self.model
        self.external = True

    def generate(self, prompt: str) -> BrainResult:
        if self.current_persona == self.target_persona:
            raise SelfReferenceError(f"self-reference blocked: {self.target_persona} CLI cannot be its own brain")
        if self.transport is None:
            raise OSError(f"{self.target_persona} CLI transport is not configured")
        return self.transport(prompt)
