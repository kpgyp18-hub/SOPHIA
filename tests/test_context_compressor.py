from sophia.context import ContextCompressor
from sophia.core.context_manager import ContextManager
from sophia.database import DatabaseManager


def database(tmp_path):
    return DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)


def test_context_compressor_round_trips_summary_and_metadata_through_sqlite(tmp_path):
    compressor = ContextCompressor(database(tmp_path))
    assert compressor.save("session-1", "earlier task summary", metadata={"task": "coding"}) == "sqlite"
    assert compressor.load("session-1").summary == "earlier task summary"
    assert compressor.load("session-1").metadata == {"task": "coding"}


def test_context_manager_dual_writes_summary_and_restores_it_without_file_state(tmp_path):
    compressor = ContextCompressor(database(tmp_path))
    manager = ContextManager(tmp_path / "context.json", compressor=compressor, session_id="session-1",
                             recent_turns=2, max_recent_chars=100, max_summary_chars=200)
    manager.add("task-1", "first request", "first result", target_path="one.py", task_type="coding")
    manager.add("task-2", "second request", "second result")
    persisted = compressor.load("session-1")
    assert persisted is not None and "first request" in persisted.summary
    restored = ContextManager(tmp_path / "missing-context.json", compressor=compressor, session_id="session-1")
    assert "Compressed earlier context" in restored.build()
    assert restored.last_file_target() == "one.py"


def test_core_wires_the_database_backed_context_compressor(tmp_path):
    from sophia.brains.dummy import DummyBrain
    from sophia.brains.ollama import BrainRegistry
    from sophia.core.orchestrator import SophiaCore

    brains = BrainRegistry(); brains.register("general", DummyBrain()); brains.register("coder", DummyBrain())
    core = SophiaCore(brains, tmp_path / "telemetry.jsonl", database=database(tmp_path))
    core.run("remember this task")
    assert core.context_compressor.load(core.session_id) is not None
