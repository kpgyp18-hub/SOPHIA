"""Declarative cross-artifact workflow and context bindings."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sophia.context.validator import semantic_version


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    skill_id: str
    depends_on: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    judge_contexts: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    version: str
    steps: tuple[WorkflowStep, ...]


class WorkflowLoader:
    def __init__(self, root: str | Path = "sophia/workflows") -> None:
        self.root = Path(root)

    def load(self) -> dict[str, WorkflowDefinition]:
        workflows: dict[str, WorkflowDefinition] = {}
        if not self.root.exists():
            return workflows
        for path in sorted(self.root.glob("**/*.yaml")):
            workflow = self.parse(path)
            if workflow.workflow_id in workflows:
                raise ValueError(f"duplicate workflow: {workflow.workflow_id}")
            workflows[workflow.workflow_id] = workflow
        return workflows

    def parse(self, path: str | Path) -> WorkflowDefinition:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict) or str(payload.get("schema_version")) != "1.0":
            raise ValueError("invalid workflow schema")
        workflow_id, version = str(payload["workflow_id"]), str(payload["version"])
        if not workflow_id.startswith("workflow."):
            raise ValueError("workflow_id must start with workflow.")
        semantic_version(version)
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("workflow requires steps")
        steps = tuple(
            WorkflowStep(
                step_id=str(step["id"]), skill_id=str(step["skill"]),
                depends_on=tuple(step.get("depends_on", ())), contexts=tuple(step.get("contexts", ())),
                judge_contexts=tuple(step.get("judge_contexts", ())),
            )
            for step in raw_steps
        )
        ids = [step.step_id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate workflow step id")
        known = set(ids)
        for step in steps:
            missing = sorted(set(step.depends_on) - known)
            if missing:
                raise ValueError(f"unknown workflow dependencies: {', '.join(missing)}")
            if any(context_id.startswith("context.judge.") for context_id in step.contexts):
                raise ValueError("judge context cannot be used for generation")
            if any(not context_id.startswith("context.judge.") for context_id in step.judge_contexts):
                raise ValueError("judge_contexts only accepts context.judge.*")
        dependencies = {step.step_id: step.depends_on for step in steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError(f"workflow dependency cycle: {step_id}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        return WorkflowDefinition(workflow_id, version, steps)
