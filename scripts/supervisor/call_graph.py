"""Allowed v1 agent-route validation."""
DIRECT_CODEX = ("CODEX",)
CLAUDE_ONLY = ("CLAUDE",)
CLAUDE_CODEX = ("CLAUDE", "CODEX")
CLAUDE_CODEX_REVIEW = ("CLAUDE", "CODEX", "CLAUDE")
EXCEPTION_GRAPH = ("CLAUDE", "CODEX", "CLAUDE", "CODEX", "CLAUDE")
ALLOWED_GRAPHS = frozenset((DIRECT_CODEX, CLAUDE_ONLY, CLAUDE_CODEX, CLAUDE_CODEX_REVIEW, EXCEPTION_GRAPH))

def validate_call_graph(agents: list[str] | tuple[str, ...]) -> tuple[bool, str | None]:
    return (True, None) if tuple(agents) in ALLOWED_GRAPHS else (False, "NEED_USER")

def validate_call_graph_prefix(agents: list[str] | tuple[str, ...]) -> tuple[bool, str | None]:
    prefix = tuple(agents)
    return (True, None) if any(graph[:len(prefix)] == prefix for graph in ALLOWED_GRAPHS) else (False, "NEED_USER")