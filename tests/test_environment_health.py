from sophia.brains.ollama import BrainRegistry, OllamaBrain
from sophia.core.health import HealthManager
from sophia.database import DatabaseManager


def test_health_manager_reports_phase_one_environment(monkeypatch, tmp_path):
    registry = BrainRegistry()
    registry.register("general", OllamaBrain("test-model", "http://ollama.test"))
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, *_):
            return b'{"models": [{"name": "test-model"}]}'

    class Process:
        stdout = "NVIDIA Test GPU, 8192 MiB\n"

    monkeypatch.setattr("sophia.core.health.urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("sophia.core.health.subprocess.run", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr("sophia.core.health.total_physical_memory_bytes", lambda: 16 * 1024 ** 3)

    checks = {check.name: check for check in HealthManager(registry, database, tmp_path).checks()}
    assert checks["workspace"].ok
    assert checks["ollama"].ok and "models=1" in checks["ollama"].detail
    assert checks["gpu"].ok and "8192 MiB" in checks["gpu"].detail
    assert checks["ram"].ok and "16.0 GiB" in checks["ram"].detail


def test_health_manager_marks_unreachable_ollama_down(monkeypatch, tmp_path):
    registry = BrainRegistry()
    registry.register("general", OllamaBrain("test-model", "http://ollama.test"))
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    monkeypatch.setattr("sophia.core.health.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))

    checks = {check.name: check for check in HealthManager(registry, database, tmp_path).checks()}
    assert not checks["ollama"].ok
    assert "offline" in checks["ollama"].detail
