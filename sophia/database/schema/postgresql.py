"""PostgreSQL v0.4 schemas.

``CORE_SCHEMA_STATEMENTS`` has no pgvector dependency and is safe for the
ordinary PostgreSQL migration.  ``VECTOR_SCHEMA_STATEMENTS`` is an explicit
follow-up that is applied only after the server has the pgvector extension.
"""
from __future__ import annotations


CORE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS runtime",
    "CREATE SCHEMA IF NOT EXISTS memory",
    "CREATE SCHEMA IF NOT EXISTS system",
    "CREATE SCHEMA IF NOT EXISTS telemetry",
    "CREATE TABLE IF NOT EXISTS runtime.sessions (session_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), status TEXT NOT NULL, active_supervisor TEXT, active_brain TEXT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb)",
    "CREATE TABLE IF NOT EXISTS runtime.session_summaries (summary_id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL REFERENCES runtime.sessions(session_id) ON DELETE CASCADE, summary TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS runtime.tasks (task_id TEXT PRIMARY KEY, session_id TEXT REFERENCES runtime.sessions(session_id), owner TEXT, reviewer TEXT, task_type TEXT, status TEXT NOT NULL, risk TEXT, complexity DOUBLE PRECISION, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS runtime.handoffs (handoff_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id) ON DELETE CASCADE, sender TEXT NOT NULL, receiver TEXT NOT NULL, status TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS runtime.agent_messages (message_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id) ON DELETE CASCADE, sender TEXT NOT NULL, receiver TEXT, content TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS memory.memories (memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, project_id TEXT, content TEXT NOT NULL, importance DOUBLE PRECISION NOT NULL DEFAULT 0.5, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_accessed TIMESTAMPTZ)",
    "CREATE TABLE IF NOT EXISTS memory.documents (document_id TEXT PRIMARY KEY, source_path TEXT, content_hash TEXT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS memory.chunks (chunk_id TEXT PRIMARY KEY, document_id TEXT REFERENCES memory.documents(document_id) ON DELETE CASCADE, content TEXT NOT NULL, ordinal INTEGER NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS memory.skills (skill_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, owner TEXT, risk TEXT, status TEXT NOT NULL, description TEXT, skill_path TEXT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, quality_score DOUBLE PRECISION NOT NULL DEFAULT 0.0, quality_samples INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "ALTER TABLE memory.skills ADD COLUMN IF NOT EXISTS quality_score DOUBLE PRECISION NOT NULL DEFAULT 0.0",
    "ALTER TABLE memory.skills ADD COLUMN IF NOT EXISTS quality_samples INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE IF NOT EXISTS memory.skill_version_history (history_id BIGSERIAL PRIMARY KEY, skill_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, owner TEXT, risk TEXT, status TEXT NOT NULL, description TEXT, skill_path TEXT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, quality_score DOUBLE PRECISION NOT NULL DEFAULT 0.0, quality_samples INTEGER NOT NULL DEFAULT 0, superseded_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS memory_skill_history_id_idx ON memory.skill_version_history(skill_id, history_id DESC)",
    "CREATE TABLE IF NOT EXISTS memory.skill_outcomes (outcome_id BIGSERIAL PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES memory.skills(skill_id) ON DELETE CASCADE, verdict TEXT NOT NULL, task_id TEXT, reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS system.brain_runs (brain_run_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), brain TEXT, provider TEXT, status TEXT NOT NULL, result JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ)",
    "CREATE TABLE IF NOT EXISTS system.worker_runs (worker_run_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), worker_type TEXT, brain TEXT, provider TEXT, status TEXT NOT NULL, result JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ)",
    "CREATE TABLE IF NOT EXISTS system.tool_runs (tool_run_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), tool_name TEXT NOT NULL, status TEXT NOT NULL, result JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ)",
    "CREATE TABLE IF NOT EXISTS system.checkpoints (checkpoint_id TEXT PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), checkpoint_type TEXT NOT NULL, workspace_path TEXT, status TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS system.workspace_events (event_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), event_type TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS system.health_events (event_id BIGSERIAL PRIMARY KEY, component TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS telemetry.token_usage (usage_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS telemetry.resource_usage (usage_id BIGSERIAL PRIMARY KEY, task_id TEXT REFERENCES runtime.tasks(task_id), cpu_percent DOUBLE PRECISION, memory_bytes BIGINT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS runtime_tasks_session_idx ON runtime.tasks(session_id)",
    "CREATE INDEX IF NOT EXISTS runtime_handoffs_task_idx ON runtime.handoffs(task_id)",
    "CREATE INDEX IF NOT EXISTS memory_memories_project_idx ON memory.memories(project_id)",
    "CREATE INDEX IF NOT EXISTS telemetry_token_usage_task_idx ON telemetry.token_usage(task_id)",
)


# Keep this in lockstep with config/system.yaml and reject incompatible vectors
# before they enter the same index.
EMBEDDING_MODEL = "qwen3-embedding:0.6b"
EMBEDDING_DIMENSIONS = 1024

VECTOR_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE TABLE IF NOT EXISTS memory.embeddings (embedding_id UUID PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory.memories(memory_id) ON DELETE CASCADE, model_name TEXT NOT NULL, embedding vector(1024) NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(memory_id, model_name))",
    "CREATE INDEX IF NOT EXISTS memory_embeddings_memory_idx ON memory.embeddings(memory_id)",
    "CREATE INDEX IF NOT EXISTS memory_embeddings_hnsw_cosine_idx ON memory.embeddings USING hnsw (embedding vector_cosine_ops)",
)
