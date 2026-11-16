"""Contract tests for the two guards installed by ``tests/conftest.py``.

The guards are the only thing standing between a regression run and the
operational PostgreSQL database, and they are installed by import side effect,
so nothing in the suite fails if a later edit removes one. These tests make the
contract explicit:

* a ``DatabaseManager`` built without naming a backend is pinned to SQLite;
* a config that *does* name one is left untouched, so the existing
  postgresql-to-sqlite fallback test keeps exercising the real path;
* ``PostgreSQLBackend`` refuses to open a socket, including through
  ``health()``, which would otherwise replay the Core DDL against whatever
  server it reaches;
* both guards defer to ``SOPHIA_PG_LIVE_TEST=1``, the opt-in flag the live
  PostgreSQL modules already gate themselves on.

No test here opens a network connection. The opt-in cases assert that the guard
delegates, with the delegate replaced by a sentinel.
"""
from __future__ import annotations

import conftest
import pytest

from sophia.database.adapters.postgresql import PostgreSQLBackend
from sophia.database.manager import DatabaseManager

LIVE_ENV = conftest.LIVE_POSTGRES_ENV


def test_manager_without_a_named_backend_is_pinned_to_sqlite():
    assert DatabaseManager().active == "sqlite"
    assert DatabaseManager({}).active == "sqlite"
    assert DatabaseManager({"health_cache_seconds": 0.0}).active == "sqlite"


def test_manager_fallback_is_pinned_too_so_selection_cannot_reach_postgresql(tmp_path):
    manager = DatabaseManager({"backends": {"sqlite": {"path": str(tmp_path / "s.db")}}})
    assert (manager.active, manager.fallback) == ("sqlite", "sqlite")
    assert manager.record("task", {"id": "guarded"}) == "sqlite"


def test_an_explicitly_named_backend_is_left_alone():
    """The fallback test depends on this; pinning it would make that test vacuous."""
    manager = DatabaseManager({"active": "postgresql", "fallback": "sqlite",
                               "backends": {"postgresql": {"host": "127.0.0.1", "port": 1}}})
    assert (manager.active, manager.fallback) == ("postgresql", "sqlite")


def test_backend_connect_refuses_without_the_opt_in():
    with pytest.raises(RuntimeError, match=LIVE_ENV):
        PostgreSQLBackend({})._connect()


def test_health_reports_the_guard_instead_of_running_ddl_against_a_server():
    health = PostgreSQLBackend({}).health()
    assert health.ok is False
    assert LIVE_ENV in health.detail


def test_migrate_cannot_replay_the_core_ddl_without_the_opt_in():
    with pytest.raises(RuntimeError, match=LIVE_ENV):
        PostgreSQLBackend({}).migrate()


def test_both_guards_defer_to_the_live_opt_in(monkeypatch):
    monkeypatch.setenv(LIVE_ENV, "1")
    sentinel = object()
    monkeypatch.setattr(conftest, "_backend_connect", lambda self: sentinel)

    assert DatabaseManager({}).active == "postgresql"  # production default, unpinned
    assert PostgreSQLBackend({})._connect() is sentinel


def test_the_opt_in_is_exactly_one(monkeypatch):
    for value in ("", "0", "true", "yes", "2"):
        monkeypatch.setenv(LIVE_ENV, value)
        assert conftest.live_postgres_requested() is False
        assert DatabaseManager({}).active == "sqlite"
