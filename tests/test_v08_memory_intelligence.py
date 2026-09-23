from __future__ import annotations

from sophia.runtime.memory import ProjectMemory


class _Embedder:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def embed(self, text: str) -> list[float]:
        return self.vectors[text]


def test_memory_tracks_importance_and_archives_lowest_effective_entry(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db", max_entries=2)
    memory.add("system", "durable system memory", confidence=0.9, trust_level="SYSTEM")
    memory.add("external", "low trust memory", confidence=0.1, trust_level="EXTERNAL")
    memory.add("new", "new memory", confidence=0.5, trust_level="USER")

    with memory._connect() as db:
        active = {row[0] for row in db.execute("SELECT text FROM memories")}
        archived = db.execute("SELECT text, importance, access_count, last_accessed_at FROM memories_archive").fetchone()
    assert active == {"durable system memory", "new memory"}
    assert archived[0] == "low trust memory"
    assert archived[1] < 0.5 and archived[2] == 0 and archived[3] is not None


def test_memory_search_records_access_and_merge_preserves_single_record(tmp_path):
    vectors = {"first": [1.0, 0.0], "same": [0.99, 0.01], "query": [1.0, 0.0]}
    memory = ProjectMemory(tmp_path / "memory.db", embedder=_Embedder(vectors))
    memory.add("task", "first", confidence=0.4)
    memory.add("task", "same", confidence=0.9)

    with memory._connect() as db:
        count, confidence, access_count = db.execute("SELECT COUNT(*), confidence, access_count FROM memories").fetchone()
    assert (count, confidence, access_count) == (1, 0.9, 0)
    assert memory.search("query") == ["first"]
    with memory._connect() as db:
        access_count, last_accessed_at = db.execute("SELECT access_count, last_accessed_at FROM memories").fetchone()
    assert access_count == 1 and last_accessed_at is not None


def test_memory_consolidation_archives_sources_with_summary_reference(tmp_path):
    vectors = {"first": [1.0, 0.0], "near": [0.85, 0.5268]}
    memory = ProjectMemory(tmp_path / "memory.db", embedder=_Embedder(vectors))
    memory.add("task", "first", confidence=0.6)
    memory.add("task", "near", confidence=0.7)

    assert memory.consolidate() == 2
    with memory._connect() as db:
        active = db.execute("SELECT id, text FROM memories").fetchall()
        archived = db.execute("SELECT text, consolidated_into FROM memories_archive ORDER BY id").fetchall()
    assert len(active) == 1 and active[0][1] == "first\nnear"
    assert archived == [("first", active[0][0]), ("near", active[0][0])]
