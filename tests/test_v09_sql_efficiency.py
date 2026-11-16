"""Locks in the database-access cost of the v0.9 runtime.

These assert *how much* work reaches the database, not what it returns. That
is deliberate: the behaviour they protect is invisible to every other test in
the suite, so without them the schema replay and the per-call health probe can
be reintroduced by an ordinary-looking refactor and nothing goes red.

Measured against a live local PostgreSQL before the change, one host-loop tick
over an 8-task goal cost 110 connections and 2,080 DDL statements; after, 29
connections and 0. The assertions below are the SQLite-backed stand-ins that
run everywhere.
"""
from __future__ import annotations

from sophia.database import DatabaseManager
from sophia.database.schema import CORE_SCHEMA_STATEMENTS
from sophia.goals import AutonomyBudget, GoalManager, PlanManager


def _database(tmp_path) -> DatabaseManager:
    return DatabaseManager({"active": "sqlite", "fallback": "sqlite"}, root=tmp_path)


def _count_calls(target, name: str) -> list[int]:
    """Replace ``target.name`` with a counting passthrough; returns the counter."""
    calls = [0]
    original = getattr(target, name)

    def counted(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    setattr(target, name, counted)
    return calls


def test_schema_is_applied_once_not_per_query(tmp_path):
    database = _database(tmp_path)
    backend = database.selected()
    migrations = _count_calls(backend, "migrate")

    goals, plans = GoalManager(database), PlanManager(database)
    goal = goals.create("efficiency", success_criteria=["measured"],
                        autonomy_budget=AutonomyBudget(), activate=True)
    for index in range(5):
        plans.create_task(goal.goal_id, f"step {index}")
    plans.list_tasks(goal.goal_id)
    goals.get(goal.goal_id)

    assert migrations[0] == 0, (
        f"schema was re-applied {migrations[0]} times during ordinary reads and writes; "
        "each replay is a fresh connection plus the whole DDL list"
    )


def test_explicit_migrate_still_always_runs(tmp_path):
    """The cache must not turn the public migration operation into a no-op."""
    database = _database(tmp_path)
    backend = database.selected()
    assert backend._schema_ready is True

    statements = _count_calls(backend, "_connect")
    backend.migrate()
    backend.migrate()
    assert statements[0] == 2, "explicit migrate() must reach the database every time"


def test_failed_health_reschedules_the_migration(tmp_path):
    """A backend that goes away must re-apply DDL on recovery, not assume it."""
    database = _database(tmp_path)
    backend = database.selected()
    assert backend._schema_ready is True

    unopenable = tmp_path / "as_a_directory"
    unopenable.mkdir()
    backend.path = unopenable  # sqlite cannot open a directory as a database
    assert backend.health().ok is False
    assert backend._schema_ready is False


def test_selected_does_not_probe_health_on_every_call(tmp_path):
    database = _database(tmp_path)
    backend = database.backends["sqlite"]
    database.active = database.fallback = "sqlite"
    probes = _count_calls(backend, "health")

    for _ in range(20):
        database.selected()

    assert probes[0] <= 1, (
        f"selected() probed backend health {probes[0]} times for 20 calls; "
        "each probe is a round trip that every read and write pays for"
    )


def test_explicit_health_check_forces_reselection(tmp_path):
    database = _database(tmp_path)
    database.selected()
    assert database._selection is not None
    database.health()
    assert database._selection is None, "an operator health check must invalidate the cached choice"


def test_zero_cache_window_restores_probe_per_call(tmp_path):
    """Callers that need the old failover timing can still opt into it."""
    database = _database(tmp_path)
    database.health_cache_seconds = 0.0
    backend = database.backends["sqlite"]
    database.active = database.fallback = "sqlite"
    probes = _count_calls(backend, "health")

    for _ in range(3):
        database.selected()

    assert probes[0] == 3


def test_foreign_keys_and_hot_queries_are_indexed():
    """Every referencing column below drives a cascade or a named query."""
    schema = "\n".join(CORE_SCHEMA_STATEMENTS)
    for index in (
        "runtime.goal_runtime_records(kind, goal_id, created_at, record_id)",
        "runtime.goal_runtime_records(kind, task_id, created_at, record_id)",
        "runtime.task_dependencies(dependency_task_id)",
        "runtime.long_tasks(parent_task_id)",
        "runtime.long_task_events(task_id, event_id)",
        "runtime.session_summaries(session_id, summary_id DESC)",
        "memory.skill_outcomes(skill_id, outcome_id DESC)",
        "memory.chunks(document_id)",
    ):
        assert index in schema, f"missing index on {index}"
