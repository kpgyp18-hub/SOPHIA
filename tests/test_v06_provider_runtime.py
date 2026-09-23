from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.brains.registry import load_registry, load_resource_manager_config
import pytest

from sophia.brains.ollama import OllamaBrain

from sophia.core.resource_manager import ModelLifecycle, ResourceManager, ResourceUsage
from sophia.brains.model_registry import ModelRegistry, ModelRole
from sophia.brains.health import ModelHealthChecker


def test_v06_config_loads_the_deployed_brain_roles():
    registry = load_registry()
    assert registry.get("fast").model == "qwen3.5:4b"
    assert registry.get("fast").think is False
    assert registry.get("general").model == "qwen3.5:9b"
    assert registry.get("reasoning").model == "phi4-reasoning:14b"
    assert registry.get("vision").model == "qwen3.5:9b"
    assert load_resource_manager_config() == {"idle_timeout_seconds": 300.0, "inference_timeout_seconds": 180.0}
    models = ModelRegistry.from_config()
    assert models.get(ModelRole.GENERAL).model == "qwen3.5:9b"
    assert models.get(ModelRole.VISION).shared_with is ModelRole.GENERAL
    assert models.get(ModelRole.EMBEDDING).model == "qwen3-embedding:0.6b"
    assert models.get(ModelRole.SAFETY_FAST).model == "llama-guard3:1b"
    assert models.get(ModelRole.SAFETY_DEEP).model == "granite4.1-guardian:8b"


def test_model_health_checks_inventory_without_loading_a_model(tmp_path):
    config = tmp_path / "system.yaml"
    config.write_text("""brains:
  general: {provider: ollama, model: general-model}
  coder: {provider: ollama, model: coder-model}
  fast: {provider: ollama, model: fast-model}
embedding: {provider: ollama, model: embedding-model}
""", encoding="utf-8")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"models": [{"name": "general-model"}, {"name": "fast-model"}]}'
    status = ModelHealthChecker(ModelRegistry.from_config(config), opener=lambda *_args, **_kwargs: Response()).check()
    assert {item.model for item in status if item.installed} == {"general-model", "fast-model"}
    assert {item.model for item in status if not item.installed} == {"coder-model", "embedding-model"}


def test_resource_manager_exposes_model_lifecycle_states():
    registry = BrainRegistry()
    general, coder = DummyBrain("general"), DummyBrain("coder")
    registry.register("general", general)
    registry.register("coder", coder)
    manager = ResourceManager(registry)

    manager.acquire("general")
    manager.mark_busy("general")
    assert ModelLifecycle.BUSY.value in manager.snapshot().values()
    manager.mark_ready("general")
    manager.acquire("coder")
    assert manager.active_name == "coder"
    assert ModelLifecycle.READY.value in manager.snapshot().values()


def test_resource_manager_measures_resources_expires_idle_and_preserves_busy_work():
    clock = [0.0]
    registry = BrainRegistry()

    class Releasable(DummyBrain):
        def __init__(self, text):
            super().__init__(text); self.unloads = 0
        def unload(self): self.unloads += 1

    general, coder = Releasable("general"), Releasable("coder")
    registry.register("general", general); registry.register("coder", coder)
    manager = ResourceManager(
        registry, idle_timeout_seconds=10, inference_timeout_seconds=5,
        clock=lambda: clock[0], usage_reader=lambda: ResourceUsage(8192, 1024, 16 * 1024 ** 3),
    )
    manager.acquire("general", priority=7)
    assert manager.status()["usage"]["vram_free_mib"] == 7168
    assert manager.status()["priorities"] == {"general": 7}
    manager.mark_busy("general")
    assert not manager.can_schedule("coder")
    with pytest.raises(TimeoutError, match="cannot pre-empt"):
        manager.acquire("coder")
    clock[0] = 6
    assert manager.check_timeouts() == ("general",)
    assert manager.snapshot()["general"] == ModelLifecycle.ERROR.value
    manager.mark_ready("general")
    clock[0] = 17
    assert manager.expire_idle()
    assert general.unloads == 1 and manager.active_name is None


def test_v071_model_swap_restores_general_and_load_failure_leaves_no_phantom_resource():
    registry = BrainRegistry()

    class Managed(DummyBrain):
        def __init__(self, name: str, *, fail_load: bool = False):
            super().__init__(name); self.model = name; self.unloads = 0; self.fail_load = fail_load
        def load(self):
            if self.fail_load:
                raise OSError("model unavailable")
        def unload(self): self.unloads += 1

    general, reasoning, broken = Managed("general"), Managed("reasoning"), Managed("broken", fail_load=True)
    registry.register("general", general); registry.register("reasoning", reasoning); registry.register("broken", broken)
    manager = ResourceManager(registry)
    manager.acquire("general")
    manager.acquire("reasoning")
    assert general.unloads == 1 and manager.active_name == "reasoning"
    assert manager.restore_general() is general
    assert reasoning.unloads == 1 and manager.active_name == "general"
    with pytest.raises(OSError, match="model unavailable"):
        manager.acquire("broken")
    assert manager.active_name is None
    assert manager.snapshot()["broken"] == ModelLifecycle.ERROR.value


def test_v071_ollama_load_warms_the_selected_model(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def opener(request, timeout):
        captured["url"], captured["timeout"] = request.full_url, timeout
        captured["body"] = request.data.decode("utf-8")
        return Response()

    monkeypatch.setattr("sophia.brains.ollama.urlopen", opener)
    OllamaBrain("general-model", "http://ollama.test").load()
    assert captured["url"].endswith("/api/generate") and captured["timeout"] == 180
    assert '"model": "general-model"' in captured["body"]
    assert '"keep_alive": "5m"' in captured["body"]
