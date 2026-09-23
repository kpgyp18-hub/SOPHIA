"""Fail-closed adapter for Granite Guardian's criteria-based judge template."""
from __future__ import annotations

import json
import re
from typing import Callable, Literal
from urllib.request import Request, urlopen

from sophia.safety.classifier import SafetyAssessment, SafetyVerdict


class GraniteGuardianAdapter:
    """Run a cold-loaded, non-authoritative deep safety judgement.

    The installed Granite Guardian template treats its initial ``system``
    message as a criteria identifier (``harm`` by default), judges the last
    user/assistant message, and returns ``<score>yes|no</score>``.  For the
    ``harm`` criterion, yes means unsafe.  Transport and parser failures are
    represented as ``unknown`` so the policy layer cannot permit an action.
    """

    _SCORE = re.compile(r"<score>\s*(yes|no)\s*</score>", re.IGNORECASE)

    def __init__(self, model: str = "granite4.1-guardian:8b", endpoint: str = "http://127.0.0.1:11434",
                 *, opener: Callable[..., object] = urlopen, timeout_seconds: int = 120) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self._opener = opener
        self.timeout_seconds = timeout_seconds

    SAFETY_PROFILE = "safety"
    JUDGE_PROFILES = frozenset({"tool_judge", "rag_groundedness", "critical_review"})

    def classify(self, message: str, *, criterion: str = "harm", profile: str = SAFETY_PROFILE,
                 subject: Literal["user", "assistant"] = "user") -> SafetyAssessment:
        if not isinstance(message, str) or not message.strip():
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model, detail="message is empty")
        if profile != self.SAFETY_PROFILE:
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model,
                                    detail=f"profile '{profile}' belongs to Judge Layer, not Safety")
        if not isinstance(criterion, str) or not criterion.strip():
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model, detail="criterion is empty")
        if subject not in {"user", "assistant"}:
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model, detail="unsupported subject")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": criterion}, {"role": subject, "content": message}],
            "stream": False,
            "keep_alive": 0,
            "think": False,
            "options": {"temperature": 0},
        }
        try:
            body = json.dumps(payload).encode("utf-8")
            request = Request(f"{self.endpoint}/api/chat", data=body, headers={"Content-Type": "application/json"})
            with self._opener(request, timeout=self.timeout_seconds) as response:
                data = json.load(response)
            raw = str(data.get("message", {}).get("content", "")).strip()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return SafetyAssessment(SafetyVerdict.UNKNOWN, model=self.model, detail=f"guardian unavailable: {error}")
        return self.parse(raw, model=self.model, criterion=criterion)

    @classmethod
    def parse(cls, raw: str, *, model: str | None = None, criterion: str = "harm") -> SafetyAssessment:
        match = cls._SCORE.search(raw)
        if match is None:
            return SafetyAssessment(SafetyVerdict.UNKNOWN, raw=raw, model=model, detail="missing Guardian score")
        if criterion != "harm":
            return SafetyAssessment(SafetyVerdict.UNKNOWN, raw=raw, model=model,
                                    detail=f"non-safety criterion '{criterion}' requires Judge Layer")
        verdict = SafetyVerdict.UNSAFE if match.group(1).lower() == "yes" else SafetyVerdict.SAFE
        return SafetyAssessment(verdict, raw=raw, model=model)
