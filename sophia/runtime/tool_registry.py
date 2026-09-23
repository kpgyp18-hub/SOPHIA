"""Declarative tool metadata and authorization over ``ToolGateway``.

The registry is deliberately policy-only.  It describes a capability before a
future permission/risk pipeline decides whether a particular invocation may
run; it never grants a brain direct execution access.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from sophia.runtime.tools import ToolGateway
from sophia.runtime.permission_engine import PermissionDecision, PermissionEngine
from sophia.runtime.risk_engine import RiskDecision, RiskEngine


RISK_LEVELS = frozenset({"R0", "R1", "R2", "R3", "R4"})
TOOLSETS = frozenset({
    "filesystem", "shell", "python", "git", "postgres", "ollama", "process",
    "system", "network", "docker", "ros2", "rag", "skill", "workspace",
    "scheduler", "browser",
})


@dataclass(frozen=True)
class ToolSpec:
    """The complete v0.7 metadata contract for one named tool capability."""

    name: str
    operation: str
    toolset: str
    risk: str
    timeout_seconds: int | None
    requires_checkpoint: bool
    requires_review: bool
    requires_user_approval: bool
    allowed_supervisors: frozenset[str]
    allowed_workers: bool = False
    network_access: bool = False
    secret_access: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.operation:
            raise ValueError("tool name and operation are required")
        if self.toolset not in TOOLSETS:
            raise ValueError(f"unsupported toolset: {self.toolset}")
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"unsupported risk level: {self.risk}")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when set")
        if not self.allowed_supervisors:
            raise ValueError("at least one supervisor must be allowed")
        if self.risk == "R4" and not (self.network_access or self.secret_access):
            raise ValueError("R4 tools must declare network_access or secret_access")
        if self.risk in {"R3", "R4"} and not self.requires_user_approval:
            raise ValueError("R3/R4 tools require user approval")


@dataclass(frozen=True)
class ToolInvocation:
    """A capability approved by the registry for one bounded dispatch."""

    spec: ToolSpec
    supervisor: str
    worker: bool
    effective_risk: str | None = None
    risk_reason: str | None = None
    permission: PermissionDecision | None = None
    task_id: str | None = None
    idempotency_key: str | None = None


_Result = TypeVar("_Result")


class ToolRegistry:
    """Authorize named capabilities; metadata remains immutable after registration."""

    DEFAULTS = (
        ToolSpec("filesystem.read", "read", "filesystem", "R0", 10, False, False, False,
                 frozenset({"blanc", "aide"}), True),
        ToolSpec("filesystem.write", "write", "filesystem", "R2", 30, True, True, False,
                 frozenset({"aide"})),
        ToolSpec("filesystem.delete", "delete", "filesystem", "R3", 30, True, True, True,
                 frozenset({"aide"})),
        ToolSpec("shell", "shell", "shell", "R2", 30, True, True, False, frozenset({"aide"})),
        ToolSpec("database", "database", "postgres", "R2", 30, True, True, False,
                 frozenset({"blanc", "aide"})),
    )

    def __init__(self, gateway: ToolGateway, specs: tuple[ToolSpec, ...] | None = None,
                 permission_engine: PermissionEngine | None = None, risk_engine: RiskEngine | None = None) -> None:
        self.gateway = gateway
        self.permission_engine = permission_engine or PermissionEngine(gateway.workspace, gateway.approval)
        self.risk_engine = risk_engine or RiskEngine()
        self._specs: dict[str, ToolSpec] = {}
        self._idempotency: dict[tuple[str, str, str], object] = {}
        for spec in specs or self.DEFAULTS:
            self.register(spec)

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Add a validated capability once; accidental replacement is denied."""
        if spec.name in self._specs:
            raise ValueError(f"tool is already registered: {spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as error:
            raise PermissionError(f"unknown tool: {name}") from error

    def authorize(self, name: str, supervisor: str, *, worker: bool = False) -> ToolSpec:
        spec = self.get(name)
        if supervisor not in spec.allowed_supervisors:
            raise PermissionError(f"{supervisor} is not allowed to use {name}")
        if worker and not spec.allowed_workers:
            raise PermissionError(f"worker is not allowed to use {name}")
        return spec

    def prepare(self, name: str, supervisor: str, *, worker: bool = False,
                request_context: str = "") -> ToolInvocation:
        """Resolve a request before it can reach a concrete tool implementation.

        Later permission, risk, safety, and review stages consume the immutable
        ``ToolInvocation.spec`` rather than accepting free-form tool metadata.
        """
        invocation = ToolInvocation(self.authorize(name, supervisor, worker=worker), supervisor, worker)
        risk = self.risk_engine.assess(invocation, request_context)
        invocation = ToolInvocation(invocation.spec, supervisor, worker, risk.risk, risk.reason)
        permission = self.permission_engine.authorize(invocation)
        return ToolInvocation(invocation.spec, supervisor, worker, risk.risk, risk.reason, permission)

    def dispatch(self, name: str, supervisor: str, handler: Callable[..., _Result], *args: object,
                 worker: bool = False, request_context: str = "", task_id: str | None = None,
                 idempotency_key: str | None = None, cancellation=None, **kwargs: object) -> _Result:
        """Run a host-owned handler only after registry authorization.

        Brains supply a tool name and arguments; only Core supplies the handler.
        This keeps execution capability out of model output while giving the
        following policy stages one mandatory integration point.
        """
        invocation = self.prepare(name, supervisor, worker=worker, request_context=request_context)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if invocation.spec.risk in {"R2", "R3", "R4"} and task_id and idempotency_key:
            record = (name, task_id, idempotency_key)
            if record in self._idempotency:
                return self._idempotency[record]  # type: ignore[return-value]
            result = handler(ToolInvocation(**{**invocation.__dict__, "task_id": task_id,
                                                "idempotency_key": idempotency_key}), *args, **kwargs)
            self._idempotency[record] = result
            return result
        return handler(ToolInvocation(**{**invocation.__dict__, "task_id": task_id,
                                          "idempotency_key": idempotency_key}), *args, **kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)
