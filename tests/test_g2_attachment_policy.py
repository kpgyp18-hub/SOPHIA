"""G2 A': Core policy decides an attachment reference before the host handler runs."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from sophia.runtime.attachment_policy import TOOL, WORK_CLASS, AttachmentPolicy
from sophia.runtime.execution_boundaries import BOUNDARIES
from sophia.runtime.permission import PermissionDenied

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("Claude", "Codex")


def audit(workspace: Path) -> list[dict]:
    path = workspace / ".sophia" / "permission_audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def spy_on_handler(policy: AttachmentPolicy, workspace: Path) -> list[int]:
    """Record how many policy decisions were already audited each time the handler ran."""
    seen: list[int] = []
    resolve = policy.registry.gateway.resolve

    def recording(path):
        seen.append(len(audit(workspace)))
        return resolve(path)

    policy.registry.gateway.resolve = recording
    return seen


def test_every_recipient_is_authorized_before_the_handler_runs(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("n", encoding="utf-8")
    policy = AttachmentPolicy(tmp_path)
    handler_runs = spy_on_handler(policy, tmp_path)
    assert policy.grant("notes.md", AGENTS) == (tmp_path / "notes.md").resolve()
    assert handler_runs == [2]                       # both decisions preceded the one handler call
    events = audit(tmp_path)
    assert [(e["tool"], e["supervisor"], e["risk"], e["allowed"]) for e in events] == [
        (TOOL, "blanc", "R0", True), (TOOL, "aide", "R0", True)]


@pytest.mark.parametrize("name,risk", [("api_token.txt", "R4"), ("db_password.txt", "R4"),
                                       ("delete_list.txt", "R3")])
def test_escalated_reference_is_refused_and_the_handler_never_runs(tmp_path, name, risk) -> None:
    (tmp_path / name).write_text("x", encoding="utf-8")
    policy = AttachmentPolicy(tmp_path)
    handler_runs = spy_on_handler(policy, tmp_path)
    with pytest.raises(PermissionDenied, match="user approval required"):
        policy.grant(name, AGENTS)
    assert handler_runs == []
    events = audit(tmp_path)
    assert [(e["risk"], e["allowed"], e["user_approval_granted"]) for e in events] == [
        (risk, False, False)]                        # refused at the first recipient


def test_explicit_approval_grants_once_and_is_not_remembered(tmp_path) -> None:
    (tmp_path / "api_token.txt").write_text("x", encoding="utf-8")
    policy = AttachmentPolicy(tmp_path)
    handler_runs = spy_on_handler(policy, tmp_path)
    assert policy.grant("api_token.txt", AGENTS, approved=True).name == "api_token.txt"
    assert [(e["risk"], e["allowed"], e["user_approval_granted"]) for e in audit(tmp_path)] == [
        ("R4", True, True), ("R4", True, True)]
    with pytest.raises(PermissionDenied):
        policy.grant("api_token.txt", AGENTS)        # the earlier approval does not carry over
    assert len(handler_runs) == 1


def test_approval_is_dropped_even_when_the_grant_fails(tmp_path) -> None:
    policy = AttachmentPolicy(tmp_path)
    with pytest.raises(PermissionError, match="escapes the workspace"):
        policy.grant("../api_token.txt", AGENTS, approved=True)
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PermissionDenied):
        policy.grant("secret.txt", AGENTS)


@pytest.mark.parametrize("supplied", ["../outside.txt", "sub/../../outside.txt"])
def test_reference_outside_the_workspace_is_refused(tmp_path, supplied) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("o", encoding="utf-8")
    with pytest.raises(PermissionError, match="escapes the workspace"):
        AttachmentPolicy(workspace).grant(supplied, AGENTS)
    with pytest.raises(PermissionError, match="escapes the workspace"):
        AttachmentPolicy(workspace).grant(str(tmp_path / "outside.txt"), AGENTS)


@pytest.mark.parametrize("recipients", [("Claude", "Gemini"), ("worker-1",), ()])
def test_unknown_or_missing_recipient_is_refused_before_any_decision(tmp_path, recipients) -> None:
    (tmp_path / "notes.md").write_text("n", encoding="utf-8")
    policy = AttachmentPolicy(tmp_path)
    handler_runs = spy_on_handler(policy, tmp_path)
    with pytest.raises(PermissionError):
        policy.grant("notes.md", recipients)
    assert handler_runs == [] and audit(tmp_path) == []


def test_single_recipient_is_authorized_under_its_own_identity(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("n", encoding="utf-8")
    AttachmentPolicy(tmp_path).grant("notes.md", ("Codex",))
    assert [e["supervisor"] for e in audit(tmp_path)] == ["aide"]


def _imports(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    names = {node.module for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and node.module is not None}
    return names | {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}


def test_boundary_declares_exactly_the_delegated_work_class() -> None:
    assert BOUNDARIES["live_chat"].core_policy_work_classes == (WORK_CLASS,)
    assert BOUNDARIES["agent_supervisor"].core_policy_work_classes == ()
    # The policy modules are used directly; the Core orchestrator is still not wired in.
    for source in ("scripts/chat.py", "scripts/supervisor/chat_session.py",
                   "sophia/runtime/attachment_policy.py"):
        assert not any(name.startswith("sophia.core") for name in _imports(source))
    assert not BOUNDARIES["live_chat"].core_orchestrator_connected
