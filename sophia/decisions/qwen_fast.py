from __future__ import annotations

import json

from sophia.brains.ollama import Brain
from sophia.decisions.base import DecisionOutcome


class QwenFastDecisionProvider:
    """Parse a strictly bounded local Qwen Fast classification response."""

    provider = "qwen_fast"
    _types = {"chat", "coding", "debug", "architecture", "review"}

    def __init__(self, brain: Brain) -> None:
        self.brain = brain

    def decide(self, user_input: str) -> DecisionOutcome:
        prompt = (
            "Classify the following request. Return JSON only with task_type "
            "(chat|coding|debug|architecture|review), complexity (0..1), confidence (0..1), and reason.\n"
            f"Request: {user_input}"
        )
        raw = self.brain.generate(prompt).text
        payload = json.loads(raw)
        task_type = str(payload["task_type"]).lower()
        if task_type not in self._types:
            raise ValueError("unsupported task type from qwen_fast")
        complexity = float(payload["complexity"])
        confidence = float(payload["confidence"])
        if not 0.0 <= complexity <= 1.0 or not 0.0 <= confidence <= 1.0:
            raise ValueError("qwen_fast confidence or complexity outside [0, 1]")
        return DecisionOutcome(task_type, complexity, confidence, self.provider, str(payload.get("reason", "")))
