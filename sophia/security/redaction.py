"""Local secret detection and masking for outbound runtime text."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redacted: bool
    kinds: tuple[str, ...]


class SecretRedactor:
    """Mask secrets before text leaves the host trust boundary.

    Patterns intentionally favour false positives over leaking a credential.
    The original value is never retained in a result or telemetry event.
    """
    _RULES = (
        ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
        ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", re.I)),
        ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I)),
        ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
        ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", re.S)),
        ("env_secret", re.compile(r"(?im)^(\s*(?:[A-Z][A-Z0-9_]*?(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY))\s*=\s*)[^\s#]+")),
        ("connection_password", re.compile(r"(?i)(://[^:\s/]+:)[^@\s/]+(@)")),
    )

    def redact(self, text: str) -> RedactionResult:
        masked, kinds = text, []
        for kind, pattern in self._RULES:
            if not pattern.search(masked):
                continue
            kinds.append(kind)
            if kind == "env_secret":
                masked = pattern.sub(r"\1[REDACTED]", masked)
            elif kind == "connection_password":
                masked = pattern.sub(r"\1[REDACTED]\2", masked)
            else:
                masked = pattern.sub(f"[REDACTED:{kind}]", masked)
        return RedactionResult(masked, bool(kinds), tuple(kinds))
