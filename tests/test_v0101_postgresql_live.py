"""Opt-in live PostgreSQL check for the v0.10.1 context registry."""
from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

from sophia.context import ContextPack, ContextRegistry, ContextTrust
from sophia.database import DatabaseManager


pytestmark = pytest.mark.skipif(
    os.environ.get("SOPHIA_PG_LIVE_TEST") != "1",
    reason="live PostgreSQL test is opt-in; set SOPHIA_PG_LIVE_TEST=1",
)

CONFIG = {
    "active": "postgresql",
    "fallback": "sqlite",
    "backends": {
        "postgresql": {
            "host": "localhost", "port": 5432, "database": "sophia", "user": "sophia",
        },
        "sqlite": {"path": "workspace/sophia/pg_live_fallback.db"},
    },
}


def test_context_registry_round_trip_on_live_postgresql():
    database = DatabaseManager(CONFIG)
    backend = database.selected()
    if backend.name != "postgresql":
        pytest.skip(f"PostgreSQL unavailable; manager selected {backend.name}")

    context_id = f"context.project.live-{uuid4().hex}"
    content = "Bounded live PostgreSQL context verification."
    pack = ContextPack(
        context_id=context_id,
        name=context_id.rsplit(".", 1)[-1],
        version="1.0.0",
        scope="project",
        category="project",
        trust=ContextTrust.INTERNAL,
        priority=50,
        max_tokens=100,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    registry = ContextRegistry(database)
    assert registry.get(context_id, include_inactive=True) is None
    try:
        assert registry.register(pack) == pack
        assert registry.get(context_id) == pack
    finally:
        database.delete_goal_runtime_record(ContextRegistry.KIND, context_id)
    assert registry.get(context_id, include_inactive=True) is None
