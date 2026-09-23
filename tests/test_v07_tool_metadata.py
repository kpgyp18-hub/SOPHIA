import pytest

from sophia.runtime.tool_registry import ToolRegistry, ToolSpec
from sophia.runtime.tools import ToolGateway


def test_default_tools_expose_the_complete_v07_metadata_contract(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    write = registry.get("filesystem.write")
    assert write.toolset == "filesystem"
    assert write.risk == "R2"
    assert write.requires_checkpoint and write.requires_review
    assert not write.requires_user_approval
    assert not write.network_access and not write.secret_access
    assert registry.get("filesystem.delete").requires_user_approval


@pytest.mark.parametrize("kwargs, message", [
    ({"toolset": "unknown"}, "unsupported toolset"),
    ({"risk": "high"}, "unsupported risk level"),
    ({"timeout_seconds": 0}, "timeout_seconds must be positive"),
    ({"risk": "R3", "requires_user_approval": False}, "R3/R4 tools require user approval"),
    ({"risk": "R4", "requires_user_approval": True}, "R4 tools must declare"),
])
def test_invalid_metadata_is_rejected(kwargs, message):
    values = dict(name="example", operation="run", toolset="python", risk="R1", timeout_seconds=10,
                  requires_checkpoint=False, requires_review=False, requires_user_approval=False,
                  allowed_supervisors=frozenset({"aide"}))
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        ToolSpec(**values)


def test_registration_is_validated_and_cannot_silently_replace_a_tool(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path), specs=())
    spec = ToolSpec("tests.run", "run", "python", "R1", 60, False, False, False, frozenset({"aide"}), True)
    assert registry.register(spec) is spec
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec)


def test_dispatch_runs_only_a_host_owned_handler_after_registry_authorization(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    received = []

    result = registry.dispatch(
        "filesystem.read", "blanc",
        lambda invocation, target: received.append((invocation, target)) or "read-result",
        "notes.txt", worker=True,
    )

    assert result == "read-result"
    invocation, target = received[0]
    assert invocation.spec.name == "filesystem.read"
    assert invocation.supervisor == "blanc" and invocation.worker is True
    assert target == "notes.txt"


def test_dispatch_denies_an_unauthorized_handler_before_it_runs(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    called = False

    def handler(_invocation):
        nonlocal called
        called = True

    with pytest.raises(PermissionError, match="not allowed"):
        registry.dispatch("filesystem.write", "blanc", handler)
    assert not called


def test_permission_engine_blocks_r3_dispatch_without_approval_and_audits_it(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    called = False

    def handler(_invocation):
        nonlocal called
        called = True

    with pytest.raises(PermissionError, match="user approval required"):
        registry.dispatch("filesystem.delete", "aide", handler)
    assert not called
    audit = (tmp_path / ".sophia" / "permission_audit.jsonl").read_text(encoding="utf-8")
    assert '"tool": "filesystem.delete"' in audit and '"allowed": false' in audit


def test_permission_engine_passes_a_single_granted_approval_to_dispatch(tmp_path):
    approvals = []
    registry = ToolRegistry(ToolGateway(tmp_path, approval=lambda description: approvals.append(description) or True))
    invocation = registry.dispatch("filesystem.delete", "aide", lambda request: request)

    assert invocation.permission is not None and invocation.permission.user_approval_granted
    assert approvals == ["filesystem.delete: delete"]


def test_risk_engine_escalates_content_without_lowering_declared_metadata_risk(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path, approval=lambda _description: True))

    invocation = registry.dispatch(
        "shell", "aide", lambda request: request,
        request_context="git push origin main using API key",
    )

    assert invocation.effective_risk == "R4"
    assert invocation.permission is not None and invocation.permission.user_approval_granted


def test_risk_engine_requires_approval_for_content_escalated_to_destructive(tmp_path):
    registry = ToolRegistry(ToolGateway(tmp_path))
    called = False

    def handler(_invocation):
        nonlocal called
        called = True

    with pytest.raises(PermissionError, match="user approval required"):
        registry.dispatch("shell", "aide", handler, request_context="git reset --hard")
    assert not called
