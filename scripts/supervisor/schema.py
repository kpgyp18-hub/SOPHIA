"""Strict machine-protocol validation for Supervisor handoffs."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Mapping

AGENTS = frozenset(("CLAUDE", "CODEX"))
RETURN_AGENTS = AGENTS | {"NONE"}
STATUSES = frozenset(("OK", "OUT_OF_SCOPE", "FAILED", "NEED_USER"))
REASON_CODES = frozenset((
    "IMPLEMENTATION_REQUIRED", "TEST_REQUIRED", "DEBUG_REQUIRED",
    "DATA_PROCESSING_REQUIRED", "DESIGN_REQUIRED", "RISK_REVIEW_REQUIRED",
    "REQUIREMENT_CLARIFICATION_REQUIRED", "SEMANTIC_REVIEW_REQUIRED",
))
HANDOFF_FIELDS = frozenset(("target_agent", "reason_code", "required_files", "required_skills", "return_to_agent", "scope", "constraints"))
OUT_FIELDS = frozenset(("status", "step_id", "handoff_request"))
HANDOFF_REQUIRED = frozenset(("target_agent", "reason_code", "required_files", "required_skills", "return_to_agent"))

class SchemaValidationError(ValueError):
    pass

def handoff_request_json_schema() -> dict[str, Any]:
    return {"type": "object", "required": sorted(HANDOFF_REQUIRED), "properties": {
        "target_agent": {"enum": sorted(AGENTS)}, "reason_code": {"enum": sorted(REASON_CODES)},
        "required_files": {"type": "array", "items": {"type": "string"}},
        "required_skills": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "return_to_agent": {"enum": sorted(RETURN_AGENTS)}, "scope": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}

def out_json_schema() -> dict[str, Any]:
    return {"type": "object", "required": ["status", "step_id"], "properties": {
        "status": {"enum": sorted(STATUSES)}, "step_id": {"type": "string"},
        "handoff_request": handoff_request_json_schema()}, "additionalProperties": False}

def structured_out_json_schema() -> dict[str, Any]:
    """Derive the strict structured-output schema required by CLI providers."""
    schema = deepcopy(out_json_schema())
    _make_optional_fields_nullable_and_required(schema)
    return schema

def normalize_structured_out_json(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove null placeholders imposed by strict structured-output providers."""
    normalized = deepcopy(dict(payload))
    if normalized.get("handoff_request") is None:
        normalized.pop("handoff_request", None)
    elif isinstance(normalized.get("handoff_request"), dict):
        request = normalized["handoff_request"]
        for field in ("scope", "constraints"):
            if request.get(field) is None:
                request.pop(field, None)
    return normalized

def _make_optional_fields_nullable_and_required(schema: dict[str, Any]) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    required = set(schema.get("required", []))
    for name, property_schema in properties.items():
        if name not in required:
            value_type = property_schema.get("type")
            if isinstance(value_type, str):
                property_schema["type"] = [value_type, "null"]
        _make_optional_fields_nullable_and_required(property_schema)
    schema["required"] = list(properties)
    schema["additionalProperties"] = False

def validate_handoff_request(request: Mapping[str, Any], *, current_agent: str | None = None, task_root: str | Path | None = None) -> None:
    _mapping(request, "handoff_request"); _only_fields(request, HANDOFF_FIELDS, "handoff_request"); _required_fields(request, HANDOFF_REQUIRED, "handoff_request")
    _enum(request["target_agent"], AGENTS, "target_agent"); _enum(request["reason_code"], REASON_CODES, "reason_code"); _enum(request["return_to_agent"], RETURN_AGENTS, "return_to_agent")
    if current_agent and request["target_agent"] == current_agent: raise SchemaValidationError("target_agent cannot equal current_agent")
    if current_agent and request["reason_code"] not in _allowed_reason_codes(current_agent): raise SchemaValidationError("reason_code is not allowed for this handoff direction")
    _strings(request["required_files"], "required_files"); _strings(request["required_skills"], "required_skills")
    if len(request["required_skills"]) > 3: raise SchemaValidationError("required_skills may contain at most 3 IDs")
    if "scope" in request and not isinstance(request["scope"], str): raise SchemaValidationError("scope must be a string")
    if "constraints" in request: _strings(request["constraints"], "constraints")
    if task_root is not None:
        for path in request["required_files"]: validate_task_path(path, task_root)

def validate_out_json(payload: Mapping[str, Any], *, current_agent: str | None = None, task_root: str | Path | None = None) -> None:
    _mapping(payload, "out.json"); _only_fields(payload, OUT_FIELDS, "out.json"); _required_fields(payload, frozenset(("status", "step_id")), "out.json")
    _enum(payload["status"], STATUSES, "status")
    if not isinstance(payload["step_id"], str): raise SchemaValidationError("step_id must be a string")
    if "handoff_request" in payload: validate_handoff_request(payload["handoff_request"], current_agent=current_agent, task_root=task_root)

def validate_task_path(value: str, task_root: str | Path) -> Path:
    if not isinstance(value, str) or not value: raise SchemaValidationError("required_files entries must be non-empty strings")
    candidate = Path(value)
    if candidate.is_absolute(): raise SchemaValidationError("required_files entries must be relative to task_root")
    root = Path(task_root).resolve(); resolved = (root / candidate).resolve()
    try: resolved.relative_to(root)
    except ValueError as exc: raise SchemaValidationError("required_files entry escapes task_root") from exc
    return resolved

def validate_required_skills(skill_ids: list[str], skills_root: str | Path) -> list[str]:
    _strings(skill_ids, "required_skills"); root = Path(skills_root)
    for skill_id in skill_ids:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", skill_id): raise SchemaValidationError("required_skills contains an invalid skill ID")
    return [skill for skill in skill_ids if not (root / skill).is_dir()]

def _allowed_reason_codes(current_agent: str) -> frozenset[str]:
    if current_agent == "CLAUDE": return frozenset(("IMPLEMENTATION_REQUIRED", "TEST_REQUIRED", "DEBUG_REQUIRED", "DATA_PROCESSING_REQUIRED"))
    if current_agent == "CODEX": return frozenset(("DESIGN_REQUIRED", "RISK_REVIEW_REQUIRED", "REQUIREMENT_CLARIFICATION_REQUIRED", "SEMANTIC_REVIEW_REQUIRED"))
    return frozenset()

def _mapping(value: Any, name: str) -> None:
    if not isinstance(value, Mapping): raise SchemaValidationError(f"{name} must be an object")
def _only_fields(value: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    extras = set(value) - allowed
    if extras: raise SchemaValidationError(f"{name} has unsupported fields: {sorted(extras)}")
def _required_fields(value: Mapping[str, Any], required: frozenset[str], name: str) -> None:
    missing = required - set(value)
    if missing: raise SchemaValidationError(f"{name} is missing fields: {sorted(missing)}")
def _enum(value: Any, choices: frozenset[str], name: str) -> None:
    if value not in choices: raise SchemaValidationError(f"{name} must be one of {sorted(choices)}")
def _strings(value: Any, name: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value): raise SchemaValidationError(f"{name} must be an array of strings")