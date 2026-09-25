from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sophia.database.base import Health


class SQLiteBackend:
    """Portable development/fallback backend; never stores credentials."""
    name = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def migrate(self) -> None:
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sophia_events (id INTEGER PRIMARY KEY, category TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_events_category ON sophia_events(category)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_skills (skill_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, version TEXT NOT NULL, owner TEXT, risk TEXT, status TEXT NOT NULL, description TEXT, skill_path TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(sophia_skills)")}
            if "quality_score" not in columns:
                db.execute("ALTER TABLE sophia_skills ADD COLUMN quality_score REAL NOT NULL DEFAULT 0.0")
            if "quality_samples" not in columns:
                db.execute("ALTER TABLE sophia_skills ADD COLUMN quality_samples INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_skill_version_history (history_id INTEGER PRIMARY KEY, skill_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, owner TEXT, risk TEXT, status TEXT NOT NULL, description TEXT, skill_path TEXT, metadata TEXT NOT NULL, quality_score REAL NOT NULL, quality_samples INTEGER NOT NULL, superseded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_skill_history_id ON sophia_skill_version_history(skill_id, history_id DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_skill_outcomes (outcome_id INTEGER PRIMARY KEY, skill_id TEXT NOT NULL, verdict TEXT NOT NULL, task_id TEXT, reason TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_session_summaries (summary_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, summary TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_session_summaries_session ON sophia_session_summaries(session_id, summary_id DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_goals (goal_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, owner TEXT NOT NULL, target_date TEXT, priority INTEGER NOT NULL, status TEXT NOT NULL, success_criteria TEXT NOT NULL, satisfied_criteria TEXT NOT NULL DEFAULT '[]', constraints TEXT NOT NULL, risk_ceiling TEXT NOT NULL, autonomy_budget TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_goals_status_priority ON sophia_goals(status, priority DESC, created_at)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_goal_events (event_id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES sophia_goals(goal_id) ON DELETE CASCADE, event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_goal_events_goal ON sophia_goal_events(goal_id, event_id)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_long_tasks (task_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES sophia_goals(goal_id) ON DELETE CASCADE, parent_task_id TEXT, title TEXT NOT NULL, description TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL, required_skills TEXT NOT NULL, required_capabilities TEXT NOT NULL, risk TEXT NOT NULL, approval_requirement INTEGER NOT NULL DEFAULT 0, deadline TEXT, retry_policy TEXT NOT NULL, checkpoint_policy TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sophia_long_tasks_goal_status ON sophia_long_tasks(goal_id, status, priority DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_task_dependencies (task_id TEXT NOT NULL REFERENCES sophia_long_tasks(task_id) ON DELETE CASCADE, dependency_task_id TEXT NOT NULL REFERENCES sophia_long_tasks(task_id), PRIMARY KEY(task_id, dependency_task_id))")
            db.execute("CREATE TABLE IF NOT EXISTS sophia_long_task_events (event_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES sophia_long_tasks(task_id) ON DELETE CASCADE, event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")

    def health(self) -> Health:
        try:
            self.migrate()
            with self._connect() as db:
                db.execute("SELECT 1").fetchone()
            return Health(self.name, True, str(self.path), False)
        except sqlite3.Error as error:
            return Health(self.name, False, str(error), False)

    def record(self, category: str, payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            db.execute("INSERT INTO sophia_events(category, payload) VALUES (?, ?)", (category, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def export(self) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db:
            rows = db.execute("SELECT category, payload, created_at FROM sophia_events ORDER BY id").fetchall()
        return [{"category": category, "payload": json.loads(payload), "created_at": created_at} for category, payload, created_at in rows]

    def import_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.record(str(record["category"]), dict(record["payload"]))

    def save_skill(self, skill: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            current = db.execute(
                "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples "
                "FROM sophia_skills WHERE skill_id = ?", (skill["skill_id"],),
            ).fetchone()
            incoming = (
                skill["skill_id"], skill["name"], skill["version"], skill.get("owner"), skill.get("risk"),
                skill["status"], skill.get("description"), skill.get("skill_path"),
                json.dumps(skill.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                float(skill.get("quality_score", 0.0)), int(skill.get("quality_samples", 0)),
            )
            if current is not None:
                comparable = (*current[:8], json.dumps(json.loads(current[8]), ensure_ascii=False, sort_keys=True), *current[9:])
                if comparable != incoming:
                    db.execute(
                        "INSERT INTO sophia_skill_version_history(skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", comparable,
                    )
            db.execute(
                "INSERT INTO sophia_skills(skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(skill_id) DO UPDATE SET name=excluded.name, version=excluded.version, owner=excluded.owner, "
                "risk=excluded.risk, status=excluded.status, description=excluded.description, skill_path=excluded.skill_path, "
                "metadata=excluded.metadata, quality_score=excluded.quality_score, quality_samples=excluded.quality_samples, "
                "updated_at=CURRENT_TIMESTAMP",
                incoming,
            )

    def get_skill(self, identifier: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples FROM sophia_skills WHERE (skill_id = ? OR name = ?)"
        parameters: tuple[str, ...] = (identifier, identifier)
        if not include_disabled:
            query += " AND status != 'disabled'"
        with self._connect() as db:
            row = db.execute(query, parameters).fetchone()
        return self._skill_record(row) if row else None

    def list_skills(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples FROM sophia_skills"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY name, version"
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [self._skill_record(row) for row in rows]

    def record_skill_outcome(self, skill_id: str, verdict: str, *, task_id: str | None = None,
                             reason: str | None = None, window: int = 20) -> tuple[float, int]:
        self.migrate()
        normalized = verdict.upper()
        if normalized not in {"PASS", "FAIL", "UNCERTAIN"}:
            raise ValueError(f"unsupported skill verdict: {verdict}")
        with self._connect() as db:
            if db.execute("SELECT 1 FROM sophia_skills WHERE skill_id = ?", (skill_id,)).fetchone() is None:
                raise KeyError(skill_id)
            db.execute(
                "INSERT INTO sophia_skill_outcomes(skill_id, verdict, task_id, reason) VALUES (?, ?, ?, ?)",
                (skill_id, normalized, task_id, reason),
            )
            rows = db.execute(
                "SELECT verdict FROM sophia_skill_outcomes WHERE skill_id = ? ORDER BY outcome_id DESC LIMIT ?",
                (skill_id, window),
            ).fetchall()
            score = sum(row[0] == "PASS" for row in rows) / len(rows)
            db.execute("UPDATE sophia_skills SET quality_score = ?, quality_samples = ?, updated_at=CURRENT_TIMESTAMP WHERE skill_id = ?",
                       (score, len(rows), skill_id))
        return score, len(rows)

    def skill_history(self, skill_id: str) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db:
            rows = db.execute(
                "SELECT skill_id, name, version, owner, risk, status, description, skill_path, metadata, quality_score, quality_samples "
                "FROM sophia_skill_version_history WHERE skill_id = ? ORDER BY history_id", (skill_id,),
            ).fetchall()
        return [self._skill_record(row) for row in rows]

    def save_context_summary(self, session_id: str, summary: str, *, metadata: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sophia_session_summaries(session_id, summary, metadata) VALUES (?, ?, ?)",
                (session_id, summary, json.dumps(metadata, ensure_ascii=False)),
            )

    def load_context_summary(self, session_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db:
            row = db.execute(
                "SELECT summary, metadata FROM sophia_session_summaries WHERE session_id = ? ORDER BY summary_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"summary": row[0], "metadata": json.loads(row[1])}

    def save_goal(self, goal: dict[str, Any], *, event_type: str,
                  event_payload: dict[str, Any]) -> None:
        self.migrate()
        values = self._goal_values(goal)
        with self._connect() as db:
            db.execute(
                "INSERT INTO sophia_goals(goal_id, title, description, owner, target_date, priority, status, success_criteria, satisfied_criteria, constraints, risk_ceiling, autonomy_budget, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(goal_id) DO UPDATE SET title=excluded.title, description=excluded.description, "
                "owner=excluded.owner, target_date=excluded.target_date, priority=excluded.priority, status=excluded.status, "
                "success_criteria=excluded.success_criteria, satisfied_criteria=excluded.satisfied_criteria, "
                "constraints=excluded.constraints, risk_ceiling=excluded.risk_ceiling, "
                "autonomy_budget=excluded.autonomy_budget, updated_at=excluded.updated_at",
                values,
            )
            db.execute(
                "INSERT INTO sophia_goal_events(goal_id, event_type, payload) VALUES (?, ?, ?)",
                (goal["goal_id"], event_type, json.dumps(event_payload, ensure_ascii=False, sort_keys=True)),
            )

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db:
            row = db.execute(f"SELECT {self._GOAL_COLUMNS} FROM sophia_goals WHERE goal_id = ?", (goal_id,)).fetchone()
        return self._goal_record(row) if row else None

    def list_goals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self.migrate()
        query = f"SELECT {self._GOAL_COLUMNS} FROM sophia_goals"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY priority DESC, created_at, goal_id"
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [self._goal_record(row) for row in rows]

    def save_goal_task(self, task: dict[str, Any], *, event_type: str,
                       event_payload: dict[str, Any]) -> None:
        self.migrate()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sophia_long_tasks(task_id, goal_id, parent_task_id, title, description, priority, status, required_skills, required_capabilities, risk, approval_requirement, deadline, retry_policy, checkpoint_policy, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET parent_task_id=excluded.parent_task_id, title=excluded.title, "
                "description=excluded.description, priority=excluded.priority, status=excluded.status, "
                "required_skills=excluded.required_skills, required_capabilities=excluded.required_capabilities, "
                "risk=excluded.risk, approval_requirement=excluded.approval_requirement, deadline=excluded.deadline, "
                "retry_policy=excluded.retry_policy, checkpoint_policy=excluded.checkpoint_policy, updated_at=excluded.updated_at",
                self._goal_task_values(task),
            )
            db.execute("DELETE FROM sophia_task_dependencies WHERE task_id = ?", (task["task_id"],))
            db.executemany(
                "INSERT INTO sophia_task_dependencies(task_id, dependency_task_id) VALUES (?, ?)",
                [(task["task_id"], dependency) for dependency in task["dependencies"]],
            )
            db.execute(
                "INSERT INTO sophia_long_task_events(task_id, event_type, payload) VALUES (?, ?, ?)",
                (task["task_id"], event_type, json.dumps(event_payload, ensure_ascii=False, sort_keys=True)),
            )

    def get_goal_task(self, task_id: str) -> dict[str, Any] | None:
        self.migrate()
        with self._connect() as db:
            row = db.execute(f"SELECT {self._GOAL_TASK_COLUMNS} FROM sophia_long_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            dependencies = [item[0] for item in db.execute(
                "SELECT dependency_task_id FROM sophia_task_dependencies WHERE task_id = ? ORDER BY dependency_task_id",
                (task_id,),
            ).fetchall()]
        return self._goal_task_record(row, dependencies)

    def list_goal_tasks(self, goal_id: str) -> list[dict[str, Any]]:
        self.migrate()
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {self._GOAL_TASK_COLUMNS} FROM sophia_long_tasks WHERE goal_id = ? ORDER BY created_at, task_id",
                (goal_id,),
            ).fetchall()
            dependency_rows = db.execute(
                "SELECT task_id, dependency_task_id FROM sophia_task_dependencies "
                "WHERE task_id IN (SELECT task_id FROM sophia_long_tasks WHERE goal_id = ?) ORDER BY dependency_task_id",
                (goal_id,),
            ).fetchall()
        dependencies: dict[str, list[str]] = {}
        for task_id, dependency in dependency_rows:
            dependencies.setdefault(task_id, []).append(dependency)
        return [self._goal_task_record(row, dependencies.get(row[0], [])) for row in rows]

    @staticmethod
    def _skill_record(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("skill_id", "name", "version", "owner", "risk", "status", "description", "skill_path", "metadata", "quality_score", "quality_samples")
        record = dict(zip(keys, row, strict=True))
        record["metadata"] = json.loads(record["metadata"])
        return record

    _GOAL_COLUMNS = "goal_id, title, description, owner, target_date, priority, status, success_criteria, satisfied_criteria, constraints, risk_ceiling, autonomy_budget, created_at, updated_at"

    @staticmethod
    def _goal_values(goal: dict[str, Any]) -> tuple[Any, ...]:
        return (
            goal["goal_id"], goal["title"], goal["description"], goal["owner"], goal.get("target_date"),
            goal["priority"], goal["status"], json.dumps(goal["success_criteria"], ensure_ascii=False),
            json.dumps(goal["satisfied_criteria"], ensure_ascii=False),
            json.dumps(goal["constraints"], ensure_ascii=False), goal["risk_ceiling"],
            json.dumps(goal["autonomy_budget"], ensure_ascii=False, sort_keys=True),
            goal["created_at"], goal["updated_at"],
        )

    @staticmethod
    def _goal_record(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("goal_id", "title", "description", "owner", "target_date", "priority", "status",
                "success_criteria", "satisfied_criteria", "constraints", "risk_ceiling",
                "autonomy_budget", "created_at", "updated_at")
        record = dict(zip(keys, row, strict=True))
        for name in ("success_criteria", "satisfied_criteria", "constraints", "autonomy_budget"):
            record[name] = json.loads(record[name])
        return record

    _GOAL_TASK_COLUMNS = "task_id, goal_id, parent_task_id, title, description, priority, status, required_skills, required_capabilities, risk, approval_requirement, deadline, retry_policy, checkpoint_policy, created_at, updated_at"

    @staticmethod
    def _goal_task_values(task: dict[str, Any]) -> tuple[Any, ...]:
        return (
            task["task_id"], task["goal_id"], task.get("parent_task_id"), task["title"], task["description"],
            task["priority"], task["status"], json.dumps(task["required_skills"], ensure_ascii=False),
            json.dumps(task["required_capabilities"], ensure_ascii=False), task["risk"],
            int(task["approval_requirement"]), task.get("deadline"),
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
            record[name] = json.loads(record[name])
        record["approval_requirement"] = bool(record["approval_requirement"])
        record["dependencies"] = dependencies
        return record
