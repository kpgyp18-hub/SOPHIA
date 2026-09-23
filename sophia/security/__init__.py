"""Prompt-injection trust boundaries for untrusted runtime context."""

from sophia.security.prompt_injection import InjectionDetector, TrustLabel, isolate_context
from sophia.security.redaction import RedactionResult, SecretRedactor

__all__ = ["InjectionDetector", "TrustLabel", "isolate_context", "RedactionResult", "SecretRedactor"]
