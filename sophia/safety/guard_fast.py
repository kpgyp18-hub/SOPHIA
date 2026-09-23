"""Adapter for Llama Guard 3's strict local chat-template result."""
from __future__ import annotations

import json
from typing import Callable
from urllib.request import Request, urlopen

from sophia.safety.classifier import SafetyAssessment, SafetyVerdict


class LlamaGuardAdapter:
    """Classify a user message without granting any execution permission.

    The installed Ollama Modelfile supplies the Llama Guard policy and accepts
    chat messages.  Its first output line must be ``safe`` or ``unsafe``;
    unexpected output and transport errors fail closed as ``unknown``.
    """

    def __init__(self, model: str = "llama-guard3:1b", endpoint: str = "http://127.0.0.1:11434",
                 *, opener: Callable[..., object] = urlopen, timeout_seconds: int = 60) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self._opener = opener
        self.timeout_seconds = timeout_seconds

    def classify(self, message: str, *, prior_messages: tuple[tuple[str, str], ...] = ()) -> SafetyAssessment:
        if not isinstance(message, str) or not message.strip():
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model, detail="message is empty")
        messages = [{"role": role, "content": content} for role, content in prior_messages]
        messages.append({"role": "user", "content": message})
        payload = {"model": self.model, "messages": messages, "stream": False, "keep_alive": 0,
                   "options": {"temperature": 0}}
        try:
            body = json.dumps(payload).encode("utf-8")
            request = Request(f"{self.endpoint}/api/chat", data=body, headers={"Content-Type": "application/json"})
            with self._opener(request, timeout=self.timeout_seconds) as response:
                data = json.load(response)
            raw = str(data.get("message", {}).get("content", "")).strip()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model, detail=f"guard unavailable: {error}")
        return self.parse(raw, model=self.model)

    @staticmethod
    def parse(raw: str, *, model: str | None = None) -> SafetyAssessment:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return SafetyAssessment(SafetyVerdict.UNKNOWN, raw=raw, model=model, detail="empty guard response")
        verdict = lines[0].lower()
        if verdict == SafetyVerdict.SAFE:
            return SafetyAssessment(SafetyVerdict.SAFE, raw=raw, model=model)
        if verdict == SafetyVerdict.UNSAFE:
            categories = tuple(category.strip() for category in lines[1].split(",") if category.strip()) if len(lines) > 1 else ()
            return SafetyAssessment(SafetyVerdict.UNSAFE, categories, raw, model)
        return SafetyAssessment(SafetyVerdict.UNKNOWN, raw=raw, model=model, detail="unrecognized guard response")
