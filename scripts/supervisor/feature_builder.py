"""Deterministic feature construction for model routing."""
from __future__ import annotations
from typing import Any, Iterable, Mapping

SEMANTIC_FEATURES = ("complexity", "requires_reasoning")

def build_features(task: Mapping[str, Any], history: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    scope = task.get("step_scope", task.get("step_id", ""))
    reason = task.get("reason_code")
    signature = task.get("validation_failure_signature")
    return {
        "task_type": str(task.get("task_type", "unspecified")),
        "complexity": task.get("complexity"),
        "requires_reasoning": task.get("requires_reasoning"),
        "file_count": len(task.get("input_paths", task.get("required_files", []))),
        "same_failure_count": same_failure_count(history, scope, reason, signature),
        "prior_model": task.get("prior_model", "NONE"),
        "test_available": bool(task.get("test_available", False)),
        "destructive": bool(task.get("destructive", False)),
        "risk": task.get("risk"),
        "review_recommended": task.get("review_recommended"),
    }

def semantic_features_missing(features: Mapping[str, Any]) -> bool:
    return any(features.get(name) is None for name in SEMANTIC_FEATURES)

def same_failure_count(history: Iterable[Mapping[str, Any]], step_scope: object, reason_code: object, signature: object) -> int:
    if reason_code is None and signature is None:
        return 0
    return sum(1 for item in history if item.get("step_scope") == step_scope and
               ((reason_code is not None and item.get("reason_code") == reason_code) or
                (signature is not None and item.get("validation_failure_signature") == signature)))