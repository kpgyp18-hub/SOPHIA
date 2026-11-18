"""Core connection C1: record what SophiaCore would decide, without acting on it.

See output/CORE_CONNECTION_STAGE1_PLAN_20261118.md. Only pure Core components are
used. Deliberately NOT used: SophiaCore() (opens the database and memory.db), the
Qwen decision provider (calls Ollama), ResourceManager (GPU probes) and health
probes. A snapshot failure is recorded as an error field and never reaches chat.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BASIS = "rules_only_no_health"  # no Qwen hint, no provider health, no resource check
_CONFIG = Path(__file__).resolve().parents[2] / "config" / "system.yaml"
_TEXT = 200


def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


class CoreShadow:
    def __init__(self, config_path: str | Path = _CONFIG, *, registry: Any = None) -> None:
        from sophia.core.router import Router
        from sophia.decisions.rules import RuleDecisionProvider
        from sophia.security.prompt_injection import InjectionDetector
        self._config = Path(config_path)
        self._registry = registry
        self._registry_error: str | None = None
        self.router = Router()
        self.rules = RuleDecisionProvider()
        self.injection = InjectionDetector()

    def _resolver(self):
        from sophia.core.provider_resolver import ProviderResolver
        if self._registry is None and self._registry_error is None:
            try:
                from sophia.brains.registry import load_registry
                self._registry = load_registry(self._config)  # builds objects only; no calls
            except Exception as exc:  # noqa: BLE001 - shadow must never break chat
                self._registry_error = type(exc).__name__[:64]
        return ProviderResolver(self._registry) if self._registry is not None else None

    def snapshot(self, message: str) -> dict[str, Any]:
        try:
            hint = self.rules.decide(message)
            decision = self.router.decide(message, hint=hint)
            task, route = decision.task, decision.escalation.route
            snap: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION, "basis": BASIS,
                "task_type": _value(task.task_type), "complexity": float(task.complexity),
                "supervisor": str(decision.supervisor), "brain": str(decision.brain),
                "context_class": str(decision.context_class), "route": _value(route),
                "route_reason": str(getattr(decision.escalation, "reason", ""))[:_TEXT],
                "injection_pattern": self.injection.detected_pattern(message),
            }
            resolver = self._resolver()
            if resolver is None:
                snap["selection"] = {"error": self._registry_error or "registry_unavailable"}
            else:
                selection = resolver.resolve(task, decision.brain, route=route)
                snap["selection"] = {"brain": str(selection.brain), "provider": str(selection.provider),
                                     "reason": str(selection.reason)[:_TEXT],
                                     "requires_user_approval": bool(selection.requires_user_approval)}
            return snap
        except Exception as exc:  # noqa: BLE001
            return {"schema_version": SCHEMA_VERSION, "basis": BASIS, "error": type(exc).__name__[:64]}


def dispatch_view(snapshot: Mapping[str, Any], *, target: str | None, explicit: bool | None) -> dict[str, Any]:
    """Compare the Core decision with what the live chat actually dispatched.

    Policy C2(a), decided 2026-11-18: an explicit @claude/@codex/@both mention is
    the user's approval to use the named external provider(s); a message sent to
    the default target is not. ``explicit=None`` means the path cannot tell.
    """
    selection = snapshot.get("selection") if isinstance(snapshot.get("selection"), Mapping) else {}
    requires = bool(selection.get("requires_user_approval")) or snapshot.get("route") == "user_approval"
    basis = {True: "explicit_mention", False: "none", None: "unknown"}[explicit if explicit in (True, False) else None]
    return {"target": target, "explicit": explicit, "core_route": snapshot.get("route"),
            "core_requires_approval": requires, "approval_basis": basis,
            # Under Core rules this dispatch would have waited for approval.
            "would_wait_under_core": requires and explicit is not True,
            "core_prefers_local": snapshot.get("route") == "local"}
