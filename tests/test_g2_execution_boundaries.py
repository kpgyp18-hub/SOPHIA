"""Guard the observed G2 B boundary against accidental cross-surface wiring."""
from __future__ import annotations

import ast
from pathlib import Path

from sophia.runtime.execution_boundaries import BOUNDARIES

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    }


def test_declared_entrypoints_exist_and_owners_are_distinct() -> None:
    assert set(BOUNDARIES) == {"live_chat", "agent_supervisor", "core_cli"}
    assert all((ROOT / boundary.entrypoint).is_file() for boundary in BOUNDARIES.values())
    assert len({boundary.state_owner for boundary in BOUNDARIES.values()}) == 3
    assert len({boundary.route_owner for boundary in BOUNDARIES.values()}) == 3


def test_live_and_supervisor_do_not_instantiate_core_orchestrator() -> None:
    live = _imports(BOUNDARIES["live_chat"].entrypoint)
    supervisor = _imports(BOUNDARIES["agent_supervisor"].entrypoint)
    core = _imports(BOUNDARIES["core_cli"].entrypoint)
    assert "sophia.runtime.tracker" in live
    assert "scripts.supervisor.supervisor" in supervisor
    assert "sophia.core.orchestrator" in core
    assert "sophia.core.orchestrator" not in live | supervisor
    assert not BOUNDARIES["live_chat"].core_orchestrator_connected
    assert not BOUNDARIES["agent_supervisor"].core_orchestrator_connected
    assert BOUNDARIES["core_cli"].core_orchestrator_connected
