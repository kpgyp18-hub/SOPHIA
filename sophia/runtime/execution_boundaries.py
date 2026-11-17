"""Observed execution ownership before G2 connects the independent surfaces.

These declarations describe current owners; they do not route or authorize work.
G2 C/A' must change a boundary and its evidence together before transferring
ownership. In particular, a shadow tracker decision is not a live dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionBoundary:
    surface: str
    entrypoint: str
    route_owner: str
    state_owner: str
    deadline_owner: str
    process_owner: str
    policy_owner: str
    persistent_store: str
    core_orchestrator_connected: bool
    # G2 A' (opt-in, SOPHIA_G2_CORE_POLICY=1): work classes that ToolRegistry /
    # PermissionEngine authorize before their host handler runs.  Everything
    # else on the surface stays with ``policy_owner``.
    core_policy_work_classes: tuple[str, ...] = ()


BOUNDARIES = {
    "live_chat": ExecutionBoundary(
        "live_chat", "scripts/chat.py", "ChatSession", "ChatTaskTracker/TaskStore",
        "ChatAdapter/Runner", "Runner", "CLI sandbox and provider permissions",
        "workspace/sophia/tasks/*.json", False,
        core_policy_work_classes=("chat.attachment_reference",),
    ),
    "agent_supervisor": ExecutionBoundary(
        "agent_supervisor", "scripts/agents.py", "Supervisor call graph", "RuntimeStore",
        "CLIAdapter/Runner", "Runner", "Supervisor guards and CLI sandbox",
        "workspace/runtime", False,
    ),
    "core_cli": ExecutionBoundary(
        "core_cli", "sophia/cli/sophia.py", "SophiaCore", "SophiaCore/TaskStore",
        "SophiaCore", "Core tool handlers", "ToolRegistry/PermissionEngine",
        "TaskStore and DatabaseManager", True,
    ),
}
