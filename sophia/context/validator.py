"""Schema and safety validation for declarative context packs."""
from __future__ import annotations

import re
from typing import Any

from sophia.context.models import ContextPack, ContextStatus, ContextTrust
from sophia.security import InjectionDetector, SecretRedactor


class ContextValidationError(ValueError):
    pass


SCOPES = frozenset({"global", "domain", "business", "project", "judge"})
CATEGORIES = frozenset({"global", "document", "data", "design", "business", "judge", "project"})
_ID_RE = re.compile(r"^context\.(global|document|data|design|business|judge|project)\.[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_PROTECTED_KEYS = frozenset({
    "allowed_tools", "tool_permission", "tool_permissions", "risk", "risk_level",
    "approval", "approval_requirement", "provider", "persona", "secret", "credential",
    "network_access", "requires_user_approval",
})


def semantic_version(version: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise ContextValidationError(f"invalid semantic version: {version}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ContextValidationError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _find_protected_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _PROTECTED_KEYS:
                return str(key)
            found = _find_protected_key(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_protected_key(nested)
            if found:
                return found
    return None


def validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "context_id", "name", "version", "scope", "category",
        "trust", "priority", "max_tokens", "status",
    }
    missing = sorted(required - metadata.keys())
    if missing:
        raise ContextValidationError(f"missing context fields: {', '.join(missing)}")
    if str(metadata["schema_version"]) != "1.0":
        raise ContextValidationError("unsupported context schema_version")
    context_id = str(metadata["context_id"])
    if not _ID_RE.fullmatch(context_id):
        raise ContextValidationError(f"invalid context_id: {context_id}")
    semantic_version(str(metadata["version"]))
    scope, category = str(metadata["scope"]), str(metadata["category"])
    if scope not in SCOPES or category not in CATEGORIES:
        raise ContextValidationError(f"invalid context scope/category: {scope}/{category}")
    try:
        ContextTrust(str(metadata["trust"]).upper())
        ContextStatus(str(metadata["status"]).lower())
    except ValueError as error:
        raise ContextValidationError(str(error)) from error
    priority, max_tokens = metadata["priority"], metadata["max_tokens"]
    if not isinstance(priority, int) or not 0 <= priority <= 100:
        raise ContextValidationError("priority must be an integer between 0 and 100")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ContextValidationError("max_tokens must be a positive integer")
    protected = _find_protected_key(metadata)
    if protected:
        raise ContextValidationError(f"context cannot define protected control: {protected}")
    applies_to = metadata.get("applies_to", {})
    if not isinstance(applies_to, dict):
        raise ContextValidationError("applies_to must be a mapping")
    for field in ("artifact_types", "skills", "projects"):
        _strings(applies_to.get(field), f"applies_to.{field}")
    for field in ("triggers", "requires", "conflicts_with"):
        _strings(metadata.get(field), field)
    return metadata


def validate_content(content: str) -> None:
    if not content.strip():
        raise ContextValidationError("CONTEXT.md cannot be empty")
    injection = InjectionDetector().detected_pattern(content)
    if injection:
        raise ContextValidationError(f"prompt injection pattern detected: {injection}")
    secrets = SecretRedactor().redact(content)
    if secrets.redacted:
        raise ContextValidationError(f"secret-like content detected: {', '.join(secrets.kinds)}")


def validate_pack(pack: ContextPack) -> None:
    """Revalidate programmatically-created packs before runtime activation."""
    metadata = {
        **pack.metadata,
        "schema_version": "1.0", "context_id": pack.context_id, "name": pack.name,
        "version": pack.version, "scope": pack.scope, "category": pack.category,
        "trust": pack.trust.value, "priority": pack.priority, "max_tokens": pack.max_tokens,
        "status": pack.status.value,
        "applies_to": {
            "artifact_types": list(pack.artifact_types), "skills": list(pack.skills),
            "projects": list(pack.projects),
        },
        "triggers": list(pack.triggers), "requires": list(pack.requires),
        "conflicts_with": list(pack.conflicts_with),
    }
    validate_metadata(metadata)
    validate_content(pack.content)
    if not re.fullmatch(r"[0-9a-f]{64}", pack.content_hash):
        raise ContextValidationError("content_hash must be a lowercase SHA-256 digest")
