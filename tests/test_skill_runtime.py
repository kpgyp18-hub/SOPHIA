from __future__ import annotations

import pytest

from sophia.database import DatabaseManager
from sophia.skills import Skill, SkillExecutor, SkillRegistry, SkillValidationError


def registry(tmp_path):
    database = DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)
    return SkillRegistry(database, library=tmp_path / "skill-library")


def test_skill_registry_persists_metadata_and_local_procedure(tmp_path):
    skills = registry(tmp_path)
    saved = skills.register(
        Skill(name="summarize-log", version="1.0.0", description="Summarize a local log.", status="active"),
        procedure="# Summarize\n1. Read the supplied log.\n2. Return key events.\n",
    )
    assert saved.skill_path is not None
    assert "Summarize" in skills.procedure(saved)
    assert skills.get(saved.skill_id) == saved
    assert skills.get("summarize-log") == saved
    assert skills.list() == [saved]


def test_skill_registry_rejects_incomplete_or_invalid_definitions(tmp_path):
    skills = registry(tmp_path)
    with pytest.raises(SkillValidationError, match="description"):
        skills.register(Skill(name="bad", version="1", description=""))
    with pytest.raises(SkillValidationError, match="risk"):
        skills.register(Skill(name="bad", version="1", description="x", risk="unsafe"))


def test_skill_executor_runs_only_registered_active_handlers(tmp_path):
    skills = registry(tmp_path)
    active = skills.register(Skill(name="double", version="1", description="Double a number.", status="active"))
    executor = SkillExecutor(skills)
    assert executor.execute(active.skill_id, {"value": 3}).status == "rejected"
    executor.register_handler("double", lambda inputs: inputs["value"] * 2)
    completed = executor.execute("double", {"value": 3})
    assert completed.status == "completed" and completed.result == 6
    skills.disable(active.skill_id)
    assert executor.execute(active.skill_id, {"value": 3}).status == "rejected"


def test_core_exposes_a_workspace_scoped_skill_runtime(tmp_path):
    from sophia.brains.dummy import DummyBrain
    from sophia.brains.ollama import BrainRegistry
    from sophia.core.orchestrator import SophiaCore

    brains = BrainRegistry(); brains.register("general", DummyBrain()); brains.register("coder", DummyBrain())
    core = SophiaCore(brains, tmp_path / "telemetry.jsonl", database=DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path))
    saved = core.skill_registry.register(Skill(name="core-skill", version="1", description="A core skill.", status="active"), procedure="# Procedure")
    assert saved.skill_path == str(tmp_path / "skills" / f"{saved.skill_id}.md")
