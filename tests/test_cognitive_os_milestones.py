from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.orchestrator import SophiaCore
from sophia.runtime.interfaces import HardwareGateway, VisionAdapter, VoiceAdapter
from sophia.runtime.memory import ProjectMemory
from sophia.runtime.tools import PermissionDenied, ToolGateway
from sophia.runtime.risk import RiskLevel, classify_shell
from sophia.runtime.budget import ContextBudget
from sophia.runtime.task import TaskStatus
from PIL import Image
from pathlib import Path


def test_dummy_brain_runs_the_dynamic_brain_path_and_persists_memory(tmp_path):
    registry = BrainRegistry()
    registry.register("general", DummyBrain("general"))
    registry.register("coder", DummyBrain("coder"))
    reasoning = DummyBrain("reasoned")
    registry.register("reasoning", reasoning)
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    task, result = core.run("architecture plan")
    assert task.task_type == "architecture"
    assert result.text == "reasoned"
    assert core.memory.search("architecture")
    assert '"estimated_cost": 0.0' in (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")


def test_workspace_tools_memory_and_interfaces_are_safe_and_local(tmp_path):
    tools = ToolGateway(tmp_path)
    tools.write_text("notes/demo.txt", "SOPHIA memory")
    assert tools.read_text("notes/demo.txt") == "SOPHIA memory"
    try:
        tools.read_text("../outside.txt")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("workspace escape must be denied")
    memory = ProjectMemory(tmp_path / "memory.db")
    memory.add("task-1", "vision and voice integration")
    assert memory.search("voice") == ["vision and voice integration"]
    assert VisionAdapter().inspect(tmp_path / "missing.png")["status"] == "missing"
    assert VoiceAdapter().transcribe("speech.wav").startswith("[STT adapter")
    assert HardwareGateway().move(1, 2, 3).z == 3


def test_project_memory_bounds_active_cache_without_discarding_archived_entries(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db", max_entries=2)
    memory.add("task-1", "old entry")
    memory.add("task-2", "middle entry")
    memory.add("task-3", "recent entry")
    with memory._connect() as db:
        active = db.execute("SELECT text FROM memories ORDER BY id").fetchall()
        archived = db.execute("SELECT text FROM memories_archive ORDER BY id").fetchall()
    assert active == [("middle entry",), ("recent entry",)]
    assert archived == [("old entry",)]


def test_v071_memory_provenance_and_embedding_mismatch_are_explicit(tmp_path):
    from sophia.runtime.memory import EMBEDDING_MODEL, EMBEDDING_VERSION, ProjectMemory

    class Embedder:
        def embed(self, _text): return [0.1] * 1024

    memory = ProjectMemory(tmp_path / "memory.db", embedder=Embedder())
    memory.add("task-1", "verified memory", source_type="tool", source_id="tool-7", session_id="session-1",
               supervisor="aide", brain="general", provider="ollama", confidence=0.9, trust_level="TOOL_VERIFIED")
    with memory._connect() as db:
        row = db.execute("SELECT source_type, source_id, session_id, supervisor, brain, provider, confidence, trust_level, embedding_model, embedding_dimension, embedding_version FROM memories").fetchone()
        db.execute("UPDATE memories SET embedding_version = 'old'")
    assert row == ("tool", "tool-7", "session-1", "aide", "general", "ollama", 0.9, "TOOL_VERIFIED", EMBEDDING_MODEL, 1024, EMBEDDING_VERSION)
    assert memory.embedding_status() == "REINDEX_REQUIRED"
    assert memory.search("verified") == ["verified memory"]  # lexical fallback never mixes embedding spaces


def test_context_budget_is_bounded_without_an_external_tokenizer():
    budget = ContextBudget(token_limit=2)
    assert len(budget.fit("x" * 20)) == 8
    assert budget.estimate("abcd") == 1


def test_coder_file_artifact_is_written_and_claim_only_response_is_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = BrainRegistry()
    registry.register("general", DummyBrain())
    registry.register("coder", DummyBrain("```python\ndef subtract(a, b):\n    return a - b\n```"))
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Add subtract to hello.py")
    assert task.status is TaskStatus.COMPLETED
    assert (tmp_path / "hello.py").read_text(encoding="utf-8").startswith("def subtract")
    registry.register("coder", DummyBrain("Successfully added subtract to hello.py"))
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Add subtract to hello.py")
    assert task.status is TaskStatus.FAILED
    assert result.text.startswith("FAILED:")


def test_existing_file_requires_matching_diff_and_preserves_prior_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    patch = """```diff
--- hello.py
+++ hello.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def subtract(a, b):
+    return a - b
```"""
    registry = BrainRegistry(); registry.register("general", DummyBrain()); registry.register("coder", DummyBrain(patch))
    task, _ = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Add subtract to hello.py")
    updated = (tmp_path / "hello.py").read_text(encoding="utf-8")
    assert task.status is TaskStatus.COMPLETED
    assert "def add" in updated and "def subtract" in updated
    backups = list((tmp_path / ".sophia" / "backups").rglob("hello.py.*.bak"))
    assert len(backups) == 1
    assert "def add" in backups[0].read_text(encoding="utf-8") and "def subtract" not in backups[0].read_text(encoding="utf-8")
    registry.register("coder", DummyBrain("```python\ndef subtract(a, b): return a - b\n```"))
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Replace hello.py")
    assert task.status is TaskStatus.FAILED
    assert "existing files require" in result.text
    assert "def add" in (tmp_path / "hello.py").read_text(encoding="utf-8")


def test_local_vision_analysis_writes_a_verifiable_json_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "sample.png"
    Image.new("RGB", (2, 1), (255, 0, 0)).save(image)
    registry = BrainRegistry(); registry.register("general", DummyBrain()); registry.register("coder", DummyBrain())
    output = Path("vision.json")
    result = SophiaCore(registry, tmp_path / "telemetry.jsonl").inspect_image(image, output)
    assert result["status"] == "analyzed"
    assert result["width"] == 2
    assert '"mean_rgb"' in (tmp_path / output).read_text(encoding="utf-8")


def test_vision_failure_is_saved_and_shell_requires_explicit_allowlist(tmp_path):
    broken = tmp_path / "broken.png"; broken.write_text("not an image", encoding="utf-8")
    output = tmp_path / "vision-failed.json"
    result = VisionAdapter().inspect_to_file(broken, output)
    assert result["status"] == "failed"
    assert '"status": "failed"' in output.read_text(encoding="utf-8")
    try:
        ToolGateway(tmp_path).shell(["python", "--version"])
    except PermissionDenied:
        pass
    else:
        raise AssertionError("shell commands require an explicit allowlist")


def test_permission_levels_classify_risk_require_approval_and_backup_existing_files(tmp_path):
    assert classify_shell("python --version") is RiskLevel.LOW
    assert classify_shell("pip install pillow") is RiskLevel.MEDIUM
    assert classify_shell("git reset --hard") is RiskLevel.HIGH
    tools = ToolGateway(tmp_path)
    tools.write_text("settings.txt", "before")
    backup = tools.write_text("settings.txt", "after")
    assert backup is not None and backup.read_text(encoding="utf-8") == "before"
    assert '"level": 2' in (tmp_path / ".sophia" / "audit.jsonl").read_text(encoding="utf-8")
    try:
        tools.delete("settings.txt")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("LEVEL 3 deletion must be denied without approval")
    approved = ToolGateway(tmp_path, approval=lambda description: description.startswith("delete"))
    approved.delete("settings.txt")
    assert not (tmp_path / "settings.txt").exists()


def test_high_risk_shell_needs_approval_even_when_allowlisted(tmp_path):
    tools = ToolGateway(tmp_path)
    try:
        tools.shell(["git", "reset", "--hard"], allowed={"git"})
    except PermissionDenied as error:
        assert "LEVEL 3 approval required" in str(error)
    else:
        raise AssertionError("high-risk shell command must require approval")


def test_file_checkpoint_restores_existing_or_new_file_and_records_lifecycle(tmp_path):
    from sophia.workspace import WorkspaceManager

    existing = tmp_path / "settings.txt"; existing.write_text("before", encoding="utf-8")
    manager = WorkspaceManager(tmp_path)
    checkpoint = manager.checkpoint_file("settings.txt")
    existing.write_text("after", encoding="utf-8")
    assert manager.rollback(checkpoint).status == "rolled_back"
    assert existing.read_text(encoding="utf-8") == "before"
    created = manager.checkpoint_file("new.txt")
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    manager.rollback(created)
    assert not (tmp_path / "new.txt").exists()
    assert manager.commit(manager.checkpoint_file("settings.txt")).status == "committed"


def test_v071_file_rollback_restores_the_original_hash(tmp_path):
    import hashlib
    from sophia.workspace import WorkspaceManager

    target = tmp_path / "settings.txt"; target.write_text("before", encoding="utf-8")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    manager = WorkspaceManager(tmp_path)
    checkpoint = manager.checkpoint_file("settings.txt")
    target.write_text("after", encoding="utf-8")
    manager.rollback(checkpoint)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected == checkpoint.sha256


def test_v071_workspace_stale_write_is_blocked_before_overwrite(tmp_path):
    from sophia.workspace import WorkspaceManager
    from sophia.workspace.manager import StaleWorkspaceError

    target = tmp_path / "settings.txt"; target.write_text("before", encoding="utf-8")
    manager = WorkspaceManager(tmp_path)
    checkpoint = manager.checkpoint_file("settings.txt")
    target.write_text("external change", encoding="utf-8")
    with __import__("pytest").raises(StaleWorkspaceError, match="STALE_WORKSPACE"):
        manager.assert_fresh(checkpoint)


def test_checkpoint_rejects_workspace_escape_and_a_held_lock(tmp_path):
    from sophia.workspace import WorkspaceManager
    from sophia.workspace.lock import WorkspaceLock, WorkspaceLockedError

    manager = WorkspaceManager(tmp_path)
    with __import__("pytest").raises(PermissionError):
        manager.checkpoint_file("../outside.txt")
    with WorkspaceLock(tmp_path / ".sophia" / "checkpoint.lock"):
        with __import__("pytest").raises(WorkspaceLockedError):
            manager.checkpoint_file("inside.txt")


def test_failed_coding_test_rolls_back_to_its_checkpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = tmp_path / "tests"; tests.mkdir()
    (tests / "test_failure.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    patch = """```diff
--- calculator.py
+++ calculator.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def subtract(a, b):
+    return a - b
```"""
    registry = BrainRegistry(); registry.register("coder", DummyBrain(patch)); registry.register("general", DummyBrain())
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Add subtract to calculator.py")
    assert task.status is TaskStatus.FAILED and "test failed:" in result.text
    assert "subtract" not in (tmp_path / "calculator.py").read_text(encoding="utf-8")
    metadata = list((tmp_path / ".sophia" / "checkpoints").glob("*/checkpoint.json"))
    assert len(metadata) == 1 and '"status": "rolled_back"' in metadata[0].read_text(encoding="utf-8")


def test_successful_coding_change_commits_its_checkpoint(tmp_path, monkeypatch):
    """The Core path commits only after the write's test stage has passed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = tmp_path / "tests"; tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    patch = """```diff
--- calculator.py
+++ calculator.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def subtract(a, b):
+    return a - b
```"""
    registry = BrainRegistry(); registry.register("coder", DummyBrain(patch)); registry.register("general", DummyBrain())
    task, _ = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Add subtract to calculator.py")
    assert task.status is TaskStatus.COMPLETED
    assert "subtract" in (tmp_path / "calculator.py").read_text(encoding="utf-8")
    metadata = list((tmp_path / ".sophia" / "checkpoints").glob("*/checkpoint.json"))
    assert len(metadata) == 1 and '"status": "committed"' in metadata[0].read_text(encoding="utf-8")


def test_no_file_artifact_rolls_back_the_prepared_checkpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    registry = BrainRegistry(); registry.register("coder", DummyBrain("No patch.")); registry.register("general", DummyBrain())
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Update calculator.py")
    assert task.status is TaskStatus.FAILED and "no complete fenced" in result.text
    metadata = list((tmp_path / ".sophia" / "checkpoints").glob("*/checkpoint.json"))
    assert len(metadata) == 1 and '"status": "rolled_back"' in metadata[0].read_text(encoding="utf-8")


def test_core_exposes_the_same_level_three_gate_to_interactive_callers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = BrainRegistry(); registry.register("general", DummyBrain()); registry.register("coder", DummyBrain())
    blocked = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    blocked.tools.write_text("remove.txt", "keep")
    try:
        blocked.delete_file("remove.txt")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("non-interactive core must deny LEVEL 3 deletion")
    approved = SophiaCore(registry, tmp_path / "interactive.jsonl", approval=lambda _: True)
    approved.delete_file("remove.txt")
    assert not (tmp_path / "remove.txt").exists()


def test_config_driven_brain_pool_and_single_active_resource_manager(tmp_path):
    from sophia.brains.registry import load_registry
    from sophia.core.resource_manager import ResourceManager

    config = tmp_path / "system.yaml"
    config.write_text("ollama_endpoint: http://example.invalid\nbrains:\n  general:\n    provider: ollama\n    model: general-model\n  coder:\n    provider: ollama\n    model: coder-model\n", encoding="utf-8")
    registry = load_registry(config)
    assert registry.get("coder").model == "coder-model"
    class ReleasableDummy(DummyBrain):
        def __init__(self): super().__init__(); self.unloaded = 0
        def unload(self): self.unloaded += 1
    first, second = ReleasableDummy(), ReleasableDummy()
    local = BrainRegistry(); local.register("general", first); local.register("coder", second)
    manager = ResourceManager(local)
    manager.acquire("general"); manager.acquire("coder")
    assert first.unloaded == 1 and manager.active_name == "coder"


def test_resource_manager_preserves_a_shared_model_and_manual_override_is_applied(tmp_path):
    from sophia.core.resource_manager import ResourceManager

    class SharedModelDummy(DummyBrain):
        def __init__(self, model: str, text: str):
            super().__init__(text); self.model, self.unloaded = model, 0
        def unload(self): self.unloaded += 1

    general = SharedModelDummy("shared", "general")
    reasoning = SharedModelDummy("shared", "reasoning")
    coder = SharedModelDummy("coder", "coder")
    registry = BrainRegistry(); registry.register("general", general); registry.register("reasoning", reasoning); registry.register("coder", coder)
    manager = ResourceManager(registry)
    manager.acquire("general"); manager.acquire("reasoning")
    assert general.unloaded == 0 and manager.active_name == "reasoning" and manager.active_model == "shared"
    manager.acquire("coder")
    assert reasoning.unloaded == 1 and manager.active_model == "coder"
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    task, result = core.run("ordinary chat", brain_override="coder")
    assert task.task_type.value == "chat" and result.text == "coder"


def test_coding_workflow_writes_runs_tests_and_has_blanc_review(tmp_path, monkeypatch):
    """v0.3 path is sequential: Aide/Coder -> filesystem -> test -> Blanc."""
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"; tests.mkdir()
    (tests / "test_smoke.py").write_text("def test_smoke():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    patch = """```python
def subtract(a, b):
    return a - b
```"""
    registry = BrainRegistry()
    registry.register("coder", DummyBrain(patch))
    registry.register("general", DummyBrain("implementation reviewed and ready"))
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    task, result = core.run("Add subtract to calculator.py")
    assert task.status is TaskStatus.COMPLETED
    assert (tmp_path / "calculator.py").exists()
    assert "[Aide] Verified write: calculator.py" in result.text
    assert "[Test]" in result.text and "1 passed" in result.text
    assert "[Blanc] implementation reviewed and ready" in result.text
    audit = (tmp_path / ".sophia" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"operation": "shell"' in audit
    snapshot = core.task_store.read(task.id)
    assert snapshot is not None
    assert [message["sender"] for message in snapshot["messages"]] == ["aide", "blanc"]
    assert snapshot["messages"][0]["metadata"]["path"] == "calculator.py"


def test_follow_up_edit_inherits_prior_target_runs_the_full_workflow(tmp_path, monkeypatch):
    """v0.3 §38: a filename-free second turn must modify the prior target."""
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"; tests.mkdir()
    (tests / "test_smoke.py").write_text("def test_smoke():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    create = "```python\ndef add(a, b):\n    return a + b\n```"
    patch = """```diff
--- calculator.py
+++ calculator.py
@@ -1,2 +1,5 @@
 def add(a, b):
     return a + b
+
+def subtract(a, b):
+    return a - b
```"""
    registry = BrainRegistry()
    registry.register("coder", DummyBrain(create))
    registry.register("general", DummyBrain("implementation reviewed and ready"))
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    first, _ = core.run("Create calculator.py")
    assert first.status is TaskStatus.COMPLETED
    registry.register("coder", DummyBrain(patch))
    second, result = core.run("Then update it")
    assert second.task_type.value == "coding" and second.status is TaskStatus.COMPLETED
    assert "def subtract" in (tmp_path / "calculator.py").read_text(encoding="utf-8")
    assert "[Aide] Verified write: calculator.py" in result.text
    assert "[Test]" in result.text and "[Blanc]" in result.text


def test_filename_free_follow_up_fails_explicitly_without_a_prior_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = BrainRegistry(); registry.register("general", DummyBrain()); registry.register("coder", DummyBrain("```python\npass\n```"))
    task, result = SophiaCore(registry, tmp_path / "telemetry.jsonl").run("Then update it")
    assert task.status is TaskStatus.FAILED
    assert "cannot determine target file" in result.text


def test_brain_failures_pause_after_two_retries_and_record_counters(tmp_path):
    class OfflineBrain:
        def __init__(self): self.calls = 0
        def generate(self, prompt):
            self.calls += 1
            raise TimeoutError("offline")

    offline = OfflineBrain()
    registry = BrainRegistry(); registry.register("general", offline); registry.register("coder", offline)
    core = SophiaCore(registry, tmp_path / "telemetry.jsonl")
    task, result = core.run("ordinary chat")
    assert task.status is TaskStatus.PAUSED
    assert task.brain_calls == 3 and task.retry_count == 2 and offline.calls == 3
    assert result.text.startswith("TASK_PAUSED:")
    snapshot = core.task_store.read(task.id)
    assert snapshot["counters"] == {"brain_calls": 3, "retry_count": 2, "review_count": 0, "tool_calls": 0}


def test_provider_telemetry_includes_model_duration_and_rate(tmp_path):
    registry = BrainRegistry(); registry.register("general", DummyBrain()); registry.register("coder", DummyBrain())
    SophiaCore(registry, tmp_path / "telemetry.jsonl").run("ordinary chat")
    import json
    event = json.loads((tmp_path / "telemetry.jsonl").read_text(encoding="utf-8"))
    assert event["provider"] == "dummy" and event["model"] == "dummy"
    assert {"brain_role", "duration_ms", "tokens_per_second", "retry_count"} <= event.keys()


def test_requested_path_allows_a_korean_particle_after_the_extension():
    from sophia.runtime.file_changes import requested_path
    assert requested_path("calculator.py에 subtract를 추가해") == "calculator.py"
