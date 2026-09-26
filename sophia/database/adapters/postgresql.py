from __future__ import annotations

import json
import os
from typing import Any

from sophia.database.base import Health
from sophia.database.migration import PostgreSQLMigrator


class PostgreSQLBackend:
    """Lazy psycopg backend. Missing drivers/services are reported, not hidden."""
    name = "postgresql"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _connect(self):
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("psycopg is not installed; install psycopg[binary] before enabling PostgreSQL") from error
        password = self.config.get("password") or os.environ.get("SOPHIA_DB_PASSWORD")
        return psycopg.connect(host=self.config.get("host", "localhost"), port=int(self.config.get("port", 5432)),
                               dbname=self.config.get("database", "sophia"), user=self.config.get("user", "sophia"),
                               password=password, connect_timeout=3)

    def migrate(self) -> None:
        with self._connect() as db, db.cursor() as cursor:
            PostgreSQLMigrator().migrate(db)
            cursor.execute("CREATE TABLE IF NOT EXISTS system.events (id BIGSERIAL PRIMARY KEY, category TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cursor.execute("CREATE INDEX IF NOT EXISTS events_category_idx ON system.events(category)")

    def migrate_vector(self) -> None:
        """Apply the separately gated pgvector schema.

        This intentionally does not run from :meth:`migrate`: installations
        without pgvector must retain a healthy PostgreSQL Core backend.
        """
        with self._connect() as db:
            PostgreSQLMigrator().migrate(db)
            PostgreSQLMigrator().migrate_vector(db)

    def health(self) -> Health:
        try:
            self.migrate()
            with self._connect() as db, db.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                vector_ready = bool(cursor.fetchone()[0])
            return Health(self.name, True, "connected", vector_ready)
        except Exception as error:  # driver/network errors are operational health, not programming errors
            return Health(self.name, False, str(error), False)

    def record(self, category: str, payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute("INSERT INTO system.events(category, payload) VALUES (%s, %s::jsonb)", (category, json.dumps(payload, ensure_ascii=False)))

    def export(self) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute("SELECT category, payload, created_at::text FROM system.events ORDER BY id")
            return [{"category": category, "payload": payload, "created_at": created_at} for category, payload, created_at in cursor.fetchall()]

    def import_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.record(str(record["category"]), dict(record["payload"]))

    def save_session(self, session_id: str, *, status: str, supervisor: str | None,
                     brain: str | None, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.sessions(session_id, status, active_supervisor, active_brain, metadata) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (session_id) DO UPDATE SET status = EXCLUDED.status, "
                "active_supervisor = EXCLUDED.active_supervisor, active_brain = EXCLUDED.active_brain, "
                "metadata = EXCLUDED.metadata, updated_at = now()",
                (session_id, status, supervisor, brain, json.dumps(metadata, ensure_ascii=False)),
            )

    def save_task(self, task_id: str, *, session_id: str, status: str, task_type: str,
                  supervisor: str, brain: str, complexity: float, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.tasks(task_id, session_id, owner, task_type, status, complexity, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (task_id) DO UPDATE SET session_id = EXCLUDED.session_id, owner = EXCLUDED.owner, "
                "task_type = EXCLUDED.task_type, status = EXCLUDED.status, complexity = EXCLUDED.complexity, "
                "metadata = EXCLUDED.metadata, updated_at = now()",
                (task_id, session_id, supervisor, task_type, status, complexity,
                 json.dumps({**metadata, "brain": brain}, ensure_ascii=False)),
            )

    def save_skill(self, skill: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples "
                "FROM memory.skills WHERE skill_id = %s FOR UPDATE", (skill["skill_id"],),
            )
            current = cursor.fetchone()
            incoming = (
                skill["skill_id"], skill["name"], skill["version"], skill.get("owner"), skill.get("risk"),
                skill["status"], skill.get("description"), skill.get("skill_path"),
                skill.get("metadata", {}), float(skill.get("quality_score", 0.0)),
                int(skill.get("quality_samples", 0)),
            )
            if current is not None:
                current_metadata = current[8] if isinstance(current[8], dict) else json.loads(current[8])
                comparable = (*current[:8], current_metadata, *current[9:])
                if comparable != incoming:
                    cursor.execute(
                        "INSERT INTO memory.skill_version_history(skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                        (*comparable[:8], json.dumps(comparable[8], ensure_ascii=False), *comparable[9:]),
                    )
            cursor.execute(
                "INSERT INTO memory.skills(skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (skill_id) DO UPDATE SET name=EXCLUDED.name, version=EXCLUDED.version, owner=EXCLUDED.owner, "
                "risk=EXCLUDED.risk, status=EXCLUDED.status, description=EXCLUDED.description, skill_path=EXCLUDED.skill_path, "
                "metadata=EXCLUDED.metadata, quality_score=EXCLUDED.quality_score, quality_samples=EXCLUDED.quality_samples, updated_at=now()",
                (skill["skill_id"], skill["name"], skill["version"], skill.get("owner"), skill.get("risk"),
                 skill["status"], skill.get("description"), skill.get("skill_path"),
                 json.dumps(skill.get("metadata", {}), ensure_ascii=False),
                 float(skill.get("quality_score", 0.0)), int(skill.get("quality_samples", 0))),
            )

    def get_skill(self, identifier: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples FROM memory.skills WHERE (skill_id = %s OR name = %s)"
        if not include_disabled:
            query += " AND status != 'disabled'"
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(query, (identifier, identifier))
            row = cursor.fetchone()
        return self._skill_record(row) if row else None

    def list_skills(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples FROM memory.skills"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = %s"
            parameters = (status,)
        query += " ORDER BY name, version"
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(query, parameters)
            rows = cursor.fetchall()
        return [self._skill_record(row) for row in rows]

    def record_skill_outcome(self, skill_id: str, verdict: str, *, task_id: str | None = None,
                             reason: str | None = None, window: int = 20) -> tuple[float, int]:
        self.migrate()
        normalized = verdict.upper()
        if normalized not in {"PASS", "FAIL", "UNCERTAIN"}:
            raise ValueError(f"unsupported skill verdict: {verdict}")
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute("SELECT 1 FROM memory.skills WHERE skill_id = %s FOR UPDATE", (skill_id,))
            if cursor.fetchone() is None:
                raise KeyError(skill_id)
            cursor.execute(
                "INSERT INTO memory.skill_outcomes(skill_id, verdict, task_id, reason) VALUES (%s, %s, %s, %s)",
                (skill_id, normalized, task_id, reason),
            )
            cursor.execute(
                "SELECT verdict FROM memory.skill_outcomes WHERE skill_id = %s ORDER BY outcome_id DESC LIMIT %s",
                (skill_id, window),
            )
            rows = cursor.fetchall()
            score = sum(row[0] == "PASS" for row in rows) / len(rows)
            cursor.execute("UPDATE memory.skills SET quality_score = %s, quality_samples = %s, updated_at=now() WHERE skill_id = %s",
                           (score, len(rows), skill_id))
        return score, len(rows)

    def skill_history(self, skill_id: str) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples "
                "FROM memory.skill_version_history WHERE skill_id = %s ORDER BY history_id", (skill_id,),
            )
            rows = cursor.fetchall()
        return [self._skill_record(row) for row in rows]

    def save_context_summary(self, session_id: str, summary: str, *, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.session_summaries(session_id, summary, metadata) VALUES (%s, %s, %s::jsonb)",
                (session_id, summary, json.dumps(metadata, ensure_ascii=False)),
            )

    def load_context_summary(self, session_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT summary, metadata FROM runtime.session_summaries WHERE session_id = %s ORDER BY summary_id DESC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {"summary": row[0], "metadata": row[1] if isinstance(row[1], dict) else json.loads(row[1])}

    def save_goal(self, goal: dict[str, Any], *, event_type: str,
                  event_payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.goals(goal_id, title, description, owner, target_date, priority, status, success_criteria, satisfied_criteria, constraints, risk_ceiling, autonomy_budget, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (goal_id) DO UPDATE SET title=EXCLUDED.title, description=EXCLUDED.description, "
                "owner=EXCLUDED.owner, target_date=EXCLUDED.target_date, priority=EXCLUDED.priority, status=EXCLUDED.status, "
                "success_criteria=EXCLUDED.success_criteria, satisfied_criteria=EXCLUDED.satisfied_criteria, "
                "constraints=EXCLUDED.constraints, risk_ceiling=EXCLUDED.risk_ceiling, "
                "autonomy_budget=EXCLUDED.autonomy_budget, updated_at=EXCLUDED.updated_at",
                (
                    goal["goal_id"], goal["title"], goal["description"], goal["owner"], goal.get("target_date"),
                    goal["priority"], goal["status"], json.dumps(goal["success_criteria"], ensure_ascii=False),
                    json.dumps(goal["satisfied_criteria"], ensure_ascii=False),
                    json.dumps(goal["constraints"], ensure_ascii=False), goal["risk_ceiling"],
                    json.dumps(goal["autonomy_budget"], ensure_ascii=False, sort_keys=True),
                    goal["created_at"], goal["updated_at"],
                ),
            )
            cursor.execute(
                "INSERT INTO runtime.goal_events(goal_id, event_type, payload) VALUES (%s, %s, %s::jsonb)",
                (goal["goal_id"], event_type, json.dumps(event_payload, ensure_ascii=False, sort_keys=True)),
            )

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(f"SELECT {self._GOAL_COLUMNS} FROM runtime.goals WHERE goal_id = %s", (goal_id,))
            row = cursor.fetchone()
        return self._goal_record(row) if row else None

    def list_goals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self.migrate()
        query = f"SELECT {self._GOAL_COLUMNS} FROM runtime.goals"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = %s"
            parameters = (status,)
        query += " ORDER BY priority DESC, created_at, goal_id"
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(query, parameters)
            rows = cursor.fetchall()
        return [self._goal_record(row) for row in rows]

    def save_goal_task(self, task: dict[str, Any], *, event_type: str,
                       event_payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.long_tasks(task_id, goal_id, parent_task_id, title, description, priority, status, required_skills, required_capabilities, risk, approval_requirement, deadline, retry_policy, checkpoint_policy, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
                "ON CONFLICT (task_id) DO UPDATE SET parent_task_id=EXCLUDED.parent_task_id, title=EXCLUDED.title, "
                "description=EXCLUDED.description, priority=EXCLUDED.priority, status=EXCLUDED.status, "
                "required_skills=EXCLUDED.required_skills, required_capabilities=EXCLUDED.required_capabilities, "
                "risk=EXCLUDED.risk, approval_requirement=EXCLUDED.approval_requirement, deadline=EXCLUDED.deadline, "
                "retry_policy=EXCLUDED.retry_policy, checkpoint_policy=EXCLUDED.checkpoint_policy, updated_at=EXCLUDED.updated_at",
                self._goal_task_values(task),
            )
            cursor.execute("DELETE FROM runtime.task_dependencies WHERE task_id = %s", (task["task_id"],))
            for dependency in task["dependencies"]:
                cursor.execute(
                    "INSERT INTO runtime.task_dependencies(task_id, dependency_task_id) VALUES (%s, %s)",
                    (task["task_id"], dependency),
                )
            cursor.execute(
                "INSERT INTO runtime.long_task_events(task_id, event_type, payload) VALUES (%s, %s, %s::jsonb)",
                (task["task_id"], event_type, json.dumps(event_payload, ensure_ascii=False, sort_keys=True)),
            )

    def get_goal_task(self, task_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(f"SELECT {self._GOAL_TASK_COLUMNS} FROM runtime.long_tasks WHERE task_id = %s", (task_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute("SELECT dependency_task_id FROM runtime.task_dependencies WHERE task_id = %s ORDER BY dependency_task_id", (task_id,))
            dependencies = [item[0] for item in cursor.fetchall()]
        return self._goal_task_record(row, dependencies)

    def list_goal_tasks(self, goal_id: str) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute(f"SELECT {self._GOAL_TASK_COLUMNS} FROM runtime.long_tasks WHERE goal_id = %s ORDER BY created_at, task_id", (goal_id,))
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT d.task_id, d.dependency_task_id FROM runtime.task_dependencies d "
                "JOIN runtime.long_tasks t ON t.task_id = d.task_id WHERE t.goal_id = %s ORDER BY d.dependency_task_id",
                (goal_id,),
            )
            dependency_rows = cursor.fetchall()
        dependencies: dict[str, list[str]] = {}
        for task_id, dependency in dependency_rows:
            dependencies.setdefault(task_id, []).append(dependency)
        return [self._goal_task_record(row, dependencies.get(row[0], [])) for row in rows]

    def save_goal_runtime_record(self, kind: str, record_id: str, payload: dict[str, Any], *,
                                 goal_id: str | None = None, task_id: str | None = None,
                                 status: str | None = None) -> None:
        now = payload.get("updated_at") or payload.get("created_at")
        if not isinstance(now, str):
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
        created_at = payload.get("created_at") if isinstance(payload.get("created_at"), str) else now
        with self._connect() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO runtime.goal_runtime_records(kind, record_id, goal_id, task_id, status, payload, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                    "ON CONFLICT(kind, record_id) DO UPDATE SET goal_id=excluded.goal_id, "
                    "task_id=excluded.task_id, status=excluded.status, payload=excluded.payload, "
                    "updated_at=excluded.updated_at",
                    (kind, record_id, goal_id, task_id, status,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at, now),
                )
            db.commit()

    def get_goal_runtime_record(self, kind: str, record_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM runtime.goal_runtime_records WHERE kind = %s AND record_id = %s",
                    (kind, record_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def list_goal_runtime_records(self, kind: str, *, goal_id: str | None = None,
                                  task_id: str | None = None,
                                  status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload FROM runtime.goal_runtime_records WHERE kind = %s"
        parameters: list[Any] = [kind]
        for column, value in (("goal_id", goal_id), ("task_id", task_id), ("status", status)):
            if value is not None:
                query += f" AND {column} = %s"
                parameters.append(value)
        query += " ORDER BY created_at, record_id"
        with self._connect() as db:
            with db.cursor() as cursor:
                cursor.execute(query, tuple(parameters))
                rows = cursor.fetchall()
        return [row[0] if isinstance(row[0], dict) else json.loads(row[0]) for row in rows]

    def delete_goal_runtime_record(self, kind: str, record_id: str) -> None:
        with self._connect() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM runtime.goal_runtime_records WHERE kind = %s AND record_id = %s",
                    (kind, record_id),
                )
            db.commit()

    @staticmethod
    def _skill_record(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("skill_id", "name", "version", "owner", "risk", "status", "description", "skill_path", "metadata", "quality_score", "quality_samples")
        return dict(zip(keys, row, strict=True))

    _GOAL_COLUMNS = "goal_id, title, description, owner, target_date::text, priority, status, success_criteria, satisfied_criteria, constraints, risk_ceiling, autonomy_budget, created_at::text, updated_at::text"

    @staticmethod
    def _goal_record(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("goal_id", "title", "description", "owner", "target_date", "priority", "status",
                "success_criteria", "satisfied_criteria", "constraints", "risk_ceiling",
                "autonomy_budget", "created_at", "updated_at")
        record = dict(zip(keys, row, strict=True))
        for name in ("success_criteria", "satisfied_criteria", "constraints", "autonomy_budget"):
            if not isinstance(record[name], (list, dict)):
                record[name] = json.loads(record[name])
        return record

    _GOAL_TASK_COLUMNS = "task_id, goal_id, parent_task_id, title, description, priority, status, required_skills, required_capabilities, risk, approval_requirement, deadline::text, retry_policy, checkpoint_policy, created_at::text, updated_at::text"

    @staticmethod
    def _goal_task_values(task: dict[str, Any]) -> tuple[Any, ...]:
        return (
            task["task_id"], task["goal_id"], task.get("parent_task_id"), task["title"], task["description"],
            task["priority"], task["status"], json.dumps(task["required_skills"], ensure_ascii=False),
            json.dumps(task["required_capabilities"], ensure_ascii=False), task["risk"],
            task["approval_requirement"], task.get("deadline"),
            json.dumps(task["retry_policy"], ensure_ascii=False, sort_keys=True),
            json.dumps(task["checkpoint_policy"], ensure_ascii=False, sort_keys=True),
            task["created_at"], task["updated_at"],
        )

    @staticmethod
    def _goal_task_record(row: tuple[Any, ...], dependencies: list[str]) -> dict[str, Any]:
        keys = ("task_id", "goal_id", "parent_task_id", "title", "description", "priority", "status",
                "required_skills", "required_capabilities", "risk", "approval_requirement", "deadline",
                "retry_policy", "checkpoint_policy", "created_at", "updated_at")
        record = dict(zip(keys, row, strict=True))
        for name in ("required_skills", "required_capabilities", "retry_policy", "checkpoint_policy"):
            if not isinstance(record[name], (list, dict)):
                record[name] = json.loads(record[name])
        record["dependencies"] = dependencies
        return record
