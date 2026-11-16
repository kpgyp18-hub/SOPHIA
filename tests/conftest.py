"""Keep the test process off the operational PostgreSQL database.

``DatabaseManager`` defaults to ``active="postgresql"`` and the PostgreSQL
backend defaults to ``localhost:5432/sophia``, so every test that builds a
``SophiaCore`` without injecting a database (roughly thirty call sites) wrote
runtime rows into the real database on any machine where the server happened to
be running. A measured regression run added 50 rows to ``runtime.tasks``.

Two independent guards close that, both installed at import time so they are in
place before collection imports any test module:

1. A ``DatabaseManager`` whose config does not name a backend is pinned to
   SQLite. Configs that name one explicitly are left alone -- tests such as
   ``test_database_manager_uses_sqlite_fallback_and_round_trips_backup`` exist
   to exercise the postgresql-to-sqlite fallback, and rewriting their intent
   would quietly make them vacuous.
2. ``PostgreSQLBackend._connect`` refuses to open a socket. This is the harder
   guarantee: it also covers ``health()``, which calls ``_ensure_schema()`` and
   therefore replays the Core DDL against whatever server it reaches, and it
   covers any backend built outside the manager.

Both guards defer to ``SOPHIA_PG_LIVE_TEST=1``, the opt-in flag the live
PostgreSQL test modules already gate themselves on. Without it, no test touches
a PostgreSQL server at all.

The production defaults are deliberately not changed: this is a test-side
boundary, so rolling it back is deleting this file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sophia.database.adapters.postgresql import PostgreSQLBackend
from sophia.database.manager import DatabaseManager

LIVE_POSTGRES_ENV = "SOPHIA_PG_LIVE_TEST"


def live_postgres_requested() -> bool:
    """True only when a run has explicitly opted in to the live database."""
    return os.environ.get(LIVE_POSTGRES_ENV) == "1"


_manager_init = DatabaseManager.__init__


def _sqlite_by_default_init(self, config: dict[str, Any] | None = None, *,
                            root: str | Path = "workspace/sophia") -> None:
    data = dict(config or {})
    if not live_postgres_requested():
        data.setdefault("active", "sqlite")
        data.setdefault("fallback", "sqlite")
    _manager_init(self, data, root=root)


_backend_connect = PostgreSQLBackend._connect


def _guarded_connect(self):
    if not live_postgres_requested():
        raise RuntimeError(
            "the test suite does not connect to PostgreSQL; "
            f"set {LIVE_POSTGRES_ENV}=1 to opt this run in to the live database"
        )
    return _backend_connect(self)


DatabaseManager.__init__ = _sqlite_by_default_init
PostgreSQLBackend._connect = _guarded_connect
