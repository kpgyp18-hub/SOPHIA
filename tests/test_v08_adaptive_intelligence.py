from __future__ import annotations

from pathlib import Path

import pytest

from sophia.brains.dummy import DummyBrain
from sophia.brains.ollama import BrainRegistry
from sophia.core.adaptive import AdaptiveScoreStore, BenchmarkResult, BrainBenchmark, FailureMemory
from sophia.core.escalation import EscalationRoute, suggest_codex_tier
from sophia.core.provider_resolver import ProviderResolver
from sophia.core.resource_manager import ResourceManager
from sophia.database import DatabaseManager
from sophia.database.adapters.postgresql import PostgreSQLBackend
from sophia.runtime.maintenance import AutonomousMaintenance
from sophia.runtime.retrieval import RetrievalRouter, RetrievalTarget
from sophia.runtime.task import Task
from sophia.runtime.tool_registry import ToolRegistry
from sophia.runtime.tools import ToolGateway
from sophia.skills import CatalogAdapter, CatalogLoader, Skill, SkillCandidateLearner, SkillRegistry
from sophia.skills.validator import SkillValidationError


def _skills(tmp_path: Path) -> SkillRegistry:
    return SkillRegistry(DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path),
                         library=tmp_path / "library")


def _write_catalog(path: Path, *, version: str = "1.0.0", tool: str = "filesystem.read",
                   body: str = "# Procedure") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""---
name: diagnose-local
version: {version}
description: Diagnose a local issue.
category: development
capability_tags:
  - debugging
allowed_tools:
  - {tool}
requires_review: false
status: active
---
{body}
""", encoding="utf-8")
    return path


def test_skill_quality_history_and_atomic_sqlite_update(tmp_path):
    registry = _skills(tmp_path)
    original = registry.register(Skill(name="quality", version="1.0.0", description="Quality test.", status="active"))
    registry.record_outcome(original.skill_id, "PASS")
    updated = registry.record_outcome(original.skill_id, "FAIL")
    assert updated.quality_score == 0.5 and updated.quality_samples == 2
    registry.register(Skill(**{**original.__dict__, "version": "1.1.0"}))
    history = registry.history(original.skill_id)
    assert history and history[-1].version == "1.0.0"


def test_postgresql_history_and_latest_upsert_share_failure_transaction(monkeypatch):
    current = ("catalog:development/x", "x", "1.0.0", "catalog", "low", "active", "x", None, {}, 0.0, 0)
    state = {"latest": current, "history": []}

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, sql, params=()):
            if sql.startswith("SELECT skill_id"):
                self.row = state["latest"]
            elif "INSERT INTO memory.skill_version_history" in sql:
                self.connection.staged.append(params)
            elif "INSERT INTO memory.skills" in sql:
                raise RuntimeError("forced upsert failure")
        def fetchone(self): return self.row

    class Connection:
        def __init__(self): self.staged = []
        def __enter__(self): return self
        def __exit__(self, exc_type, *_args):
            if exc_type is None: state["history"].extend(self.staged)
            self.staged.clear()
            return False
        def cursor(self):
            cursor = Cursor(); cursor.connection = self; cursor.row = None; return cursor

    backend = PostgreSQLBackend({})
    monkeypatch.setattr(backend, "migrate", lambda: None)
    monkeypatch.setattr(backend, "_connect", Connection)
    with pytest.raises(RuntimeError, match="forced"):
        backend.save_skill({"skill_id": current[0], "name": "x", "version": "1.1.0", "owner": "catalog",
                            "risk": "low", "status": "active", "description": "x", "metadata": {}})
    assert state["latest"] == current and state["history"] == []


def test_catalog_derives_risk_rejects_unknown_tools_and_version_conflicts(tmp_path):
    registry = _skills(tmp_path)
    tools = ToolRegistry(ToolGateway(tmp_path))
    path = _write_catalog(tmp_path / "catalog/development/diagnose-local/SKILL.md", tool="filesystem.write")
    loader, adapter = CatalogLoader(tmp_path / "catalog"), CatalogAdapter(registry, tools)
    document = loader.load()[0]
    saved = adapter.register(document)
    assert saved.skill_id == "catalog:development/diagnose-local"
    assert saved.risk == "medium" and saved.metadata["computed_risk"] == "R2"
    path.write_text(path.read_text(encoding="utf-8").replace("# Procedure", "# Changed"), encoding="utf-8")
    with pytest.raises(SkillValidationError, match="hash conflict"):
        adapter.register(loader.load()[0])
    unknown = _write_catalog(tmp_path / "catalog/development/unknown/SKILL.md", tool="missing.tool")
    with pytest.raises(PermissionError, match="unknown tool"):
        adapter.register(loader.parse(unknown))


def test_skill_learning_creates_candidate_but_requires_explicit_approval(tmp_path):
    registry = _skills(tmp_path)
    learner = SkillCandidateLearner(registry, minimum_successes=3)
    assert learner.observe_success("coding", "run tests") is None
    assert learner.observe_success("coding", "run tests") is None
    candidate = learner.observe_success("coding", "run tests")
    assert candidate is not None and candidate.status == "candidate"
    with pytest.raises(PermissionError, match="explicit approval"):
        learner.activate(candidate.skill_id, approval=None)
    assert learner.activate(candidate.skill_id, approval=lambda _skill: True).status == "active"


def test_adaptive_ranking_only_reorders_eligible_candidates():
    registry = BrainRegistry()
    for name in ("reasoning", "general"):
        brain = DummyBrain(name); brain.provider = name; registry.register(name, brain)
    resolver = ProviderResolver(registry)
    task = Task("reason")
    assert resolver.resolve(task, "reasoning", route=EscalationRoute.LOCAL).brain == "reasoning"
    selected = resolver.resolve(task, "reasoning", route=EscalationRoute.LOCAL,
                                adaptive_scores={"reasoning": 0.1, "general": 0.9})
    assert selected.brain == "general"
    selected = resolver.resolve(task, "reasoning", route=EscalationRoute.LOCAL,
                                health={"reasoning": True, "general": False},
                                adaptive_scores={"reasoning": 0.1, "general": 1.0})
    assert selected.brain == "reasoning" and selected.excluded["general"] == "provider unhealthy"


def test_benchmark_resolves_before_resource_acquire():
    events = []
    registry = BrainRegistry(); brain = DummyBrain("general"); brain.provider = "ollama"; registry.register("general", brain)
    resolver = ProviderResolver(registry)

    class Resources(ResourceManager):
        def can_schedule(self, name, *, priority=0): events.append(("eligible", name)); return True
        def acquire(self, name, *, priority=0): events.append(("acquire", name)); return registry.get(name)
        def release(self, name=None): events.append(("release", name)); return True

    scores = AdaptiveScoreStore()
    selection, result = BrainBenchmark(resolver, Resources(registry), scores).run(
        Task("chat"), "general", lambda _brain: BenchmarkResult("general", 0.8, 20, True),
    )
    assert selection.brain == result.brain == "general"
    assert events[:2] == [("eligible", "general"), ("acquire", "general")]
    assert scores.scores()["general"] > 0.8


def test_failure_learning_supplies_escalation_count_automatically():
    memory = FailureMemory()
    task = Task("fix")
    memory.record(task_type=task.task_type.value, brain="coder", reason="same")
    calls = []
    suggest_codex_tier(task, failure_lookup=lambda current: calls.append(current.id) or memory.count(task_type=current.task_type.value))
    assert calls == [task.id]


def test_retrieval_router_and_r1_maintenance_boundary(tmp_path):
    router = RetrievalRouter()
    assert router.classify("find a skill procedure") is RetrievalTarget.SKILL
    assert router.classify("why was this decision made") is RetrievalTarget.DECISION_TRACE
    assert router.classify("remember the earlier result") is RetrievalTarget.MEMORY
    skills = _skills(tmp_path)
    skill = skills.register(Skill(name="weak", version="1", description="weak", status="active"))
    for verdict in ("FAIL", "FAIL", "PASS"):
        skills.record_outcome(skill.skill_id, verdict)
    report = AutonomousMaintenance(__import__("sophia.runtime.memory", fromlist=["ProjectMemory"]).ProjectMemory(tmp_path / "memory.db"), skills).run_local()
    assert report.maximum_risk == "R1" and report.low_quality_skill_candidates == (skill.skill_id,)
