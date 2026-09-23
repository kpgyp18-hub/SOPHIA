from sophia.runtime.memory import ProjectMemory


class _Embedder:
    values = {
        "database indexing": [1.0, 0.0],
        "image classification": [0.0, 1.0],
        "find database": [0.9, 0.1],
    }

    def embed(self, text: str) -> list[float]:
        return self.values[text]


def test_memory_uses_semantic_similarity_before_lexical_fallback(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db", embedder=_Embedder())
    memory.add("one", "database indexing")
    memory.add("two", "image classification")
    assert memory.search("find database")[0] == "database indexing"


def test_memory_keeps_lexical_retrieval_when_embedder_is_unavailable(tmp_path):
    class Offline:
        def embed(self, text):
            raise TimeoutError("cold model")

    memory = ProjectMemory(tmp_path / "memory.db", embedder=Offline())
    memory.add("one", "durable local memory")
    assert memory.search("memory") == ["durable local memory"]
