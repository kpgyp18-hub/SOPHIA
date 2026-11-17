"""G2 A': Core policy enforcement for one read-only live-chat work class.

``/attach`` grants the session's agents a reference to a workspace file.  With
``SOPHIA_G2_CORE_POLICY=1`` that grant is made only through ``ToolRegistry``:
registry authorization, RiskEngine escalation and PermissionEngine approval and
audit all run before the host handler that resolves the path.

This is the whole scope.  The agents' own CLI tools stay under their sandbox
and provider permissions, and nothing here routes, starts or supervises an agent.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .permission import PermissionDenied
from .tool_registry import ToolInvocation, ToolRegistry
from .tools import ToolGateway

FLAG_ENV = "SOPHIA_G2_CORE_POLICY"
WORK_CLASS = "chat.attachment_reference"
TOOL = "filesystem.read"
# Live-chat agent names -> the supervisor identities the registry authorizes.
_SUPERVISOR_FOR_AGENT = {"claude": "blanc", "codex": "aide"}


def enabled() -> bool:
    return os.environ.get(FLAG_ENV) == "1"


class OutsideWorkspace(PermissionError):
    """The authorized handler refused the path itself: it leaves the workspace."""


class AttachmentPolicy:
    def __init__(self, workspace: str | Path) -> None:
        # Approval is the user's explicit, per-request statement; it is never
        # remembered for the next request.
        self._approved = False
        self.registry = ToolRegistry(
            ToolGateway(workspace, approval=lambda description: self._approved))

    def grant(self, supplied: str, recipients: Iterable[str], *, approved: bool = False) -> Path:
        """The workspace path the recipients may be pointed at.

        Every recipient is authorized before the handler touches the path, so a
        refusal for any of them (``PermissionError``) grants nothing.
        """
        supervisors = []
        for name in recipients:
            supervisor = _SUPERVISOR_FOR_AGENT.get(str(name).lower())
            if supervisor is None:
                raise PermissionError(f"no Core supervisor identity for agent {name!r}")
            supervisors.append(supervisor)
        if not supervisors:
            raise PermissionError("an attachment needs at least one recipient")
        self._approved = bool(approved)
        try:
            for supervisor in supervisors[:-1]:
                self.registry.prepare(TOOL, supervisor, request_context=supplied)
            return self.registry.dispatch(TOOL, supervisors[-1], self._resolve, supplied,
                                          request_context=supplied)
        finally:
            self._approved = False

    def _resolve(self, invocation: ToolInvocation, supplied: str) -> Path:
        """Host-owned handler: reached only with an authorized invocation."""
        try:
            return self.registry.gateway.resolve(supplied)
        except PermissionDenied as exc:
            raise OutsideWorkspace(str(exc)) from exc
