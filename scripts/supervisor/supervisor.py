"""Sequential Supervisor Core with runtime and foreground presentation wiring."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapter import AgentAdapter
from .call_graph import CLAUDE_CODEX, CLAUDE_CODEX_REVIEW, CLAUDE_ONLY, DIRECT_CODEX, EXCEPTION_GRAPH, validate_call_graph, validate_call_graph_prefix
from .fake_adapter import FakeAdapter
from .loop_guard import LoopGuard
from .presentation import (emit_done, emit_failed, emit_handoff, emit_handoff_received,
                           emit_message, emit_need_user, emit_review, emit_running, emit_start, emit_test,
                           emit_timing)
from .runtime_store import RuntimeStore
from .runner import Runner
from .writer_lock import WriterLock
from .schema import SchemaValidationError, validate_out_json, validate_required_skills
from .feature_builder import build_features
from .model_budget import ModelBudget
from .model_router import ModelRouter
from .presentation import emit_model
from .timing import TimingLog

_ROLE_FOR_REASON = {
    "IMPLEMENTATION_REQUIRED": "exec", "TEST_REQUIRED": "validate", "DEBUG_REQUIRED": "exec",
    "DATA_PROCESSING_REQUIRED": "exec", "DESIGN_REQUIRED": "plan", "RISK_REVIEW_REQUIRED": "review",
    "REQUIREMENT_CLARIFICATION_REQUIRED": "plan", "SEMANTIC_REVIEW_REQUIRED": "review",
}
_INITIAL_ROLE = {"CODEX": "exec", "CLAUDE": "plan"}

class Supervisor:
    def __init__(self, runtime_root: str | Path, skills_root: str | Path, adapters: Mapping[str, AgentAdapter], *, model_router: ModelRouter | None = None, model_budget: ModelBudget | None = None, codex_feature_inputs: Mapping[str, Any] | None = None, global_budget_check: Callable[[LoopGuard], bool] | None = None, initial_description: str | None = None) -> None:
        self.store = RuntimeStore(runtime_root)
        self.skills_root = Path(skills_root)
        self.adapters = dict(adapters)
        self.model_router = model_router
        self.model_budget = model_budget or ModelBudget()
        self.codex_feature_inputs = dict(codex_feature_inputs or {})
        self.global_budget_check = global_budget_check or (lambda guard: guard.agent_handoffs < 4)
        self.initial_description = initial_description

    def run(self, task_id: str, route: tuple[str, ...] | list[str]) -> dict[str, Any]:
        route = tuple(route)
        valid, _ = validate_call_graph(route)
        if not valid or not route:
            return self._terminal(task_id, route, "NEED_USER", "undefined call graph")
        existing = self.store.load_state(task_id)
        if existing and existing.get("status") == "DONE":
            return existing
        initial_agent = route[0]
        guard = LoopGuard(current_agent=initial_agent)
        current_agent, current_role = initial_agent, _INITIAL_ROLE[initial_agent]
        step_number = 1
        previous_step = "NONE"
        next_description = self.initial_description
        state = self._state(task_id, route, "RUNNING", current_agent, current_role, step_number, guard)
        self.store.write_state(task_id, state)
        emit_start(current_agent, current_role, step=step_number, total=len(route))

        while True:
            step_id = f"{step_number:02d}-{current_role}"
            task = {"step_id": step_id, "agent": current_agent, "role": current_role,
                    "step_number": step_number, "step_total": len(route),
                    "required_skills": [], "input_paths": [], "output_path": "",
                    "previous_step": previous_step, "return_to": "NONE"}
            if next_description:
                task["description"] = next_description
            if current_agent == "CODEX" and self.model_router is not None:
                feature_input = self.codex_feature_inputs.get(step_id, self.codex_feature_inputs.get("*", {}))
                feature_source = {**dict(feature_input), **task}
                features = build_features(feature_source)
                decision = self.model_router.decide(features)
                if decision.status != "READY" or decision.model_tier is None:
                    return self._terminal(task_id, route, "NEED_USER", decision.reason, guard, current_agent, current_role, step_number)
                if not self.model_budget.can_execute(decision.model_tier, global_budget_pass=self.global_budget_check(guard)):
                    return self._terminal(task_id, route, "NEED_USER", "global or model budget exhausted", guard, current_agent, current_role, step_number)
                self.model_budget.record_call(decision.model_tier, str(features["prior_model"]))
                task["model_tier"] = decision.model_tier
                task["task_features"] = features
                emit_model(current_agent, decision.model_tier)
                self.store.write_state(task_id, self._state(task_id, route, "RUNNING", current_agent, current_role, step_number, guard, decision.model_tier))
            self.store.write_step(task_id, step_number, current_role, "task", task)
            if current_role == "validate": emit_test(current_agent)
            elif current_role == "review": emit_review(current_agent)
            emit_running(current_agent, current_role, step=step_number, total=len(route))
            try:
                output = self.adapters[current_agent].execute(task)
                validate_out_json(output, current_agent=current_agent, task_root=self.store.task_root(task_id))
                if output.get("step_id") != step_id:
                    raise SchemaValidationError("out.json step_id does not match the active step")
                self.store.write_step(task_id, step_number, current_role, "out", output)
            except Exception as exc:
                return self._terminal(task_id, route, "FAILED", str(exc), guard, current_agent, current_role, step_number)
            timing = getattr(self.adapters[current_agent], "last_timing", None)
            if timing is not None and timing.total is not None:
                emit_timing(timing.summary())
            emit_message(current_agent, output)

            if output["status"] != "OK":
                status = "NEED_USER" if output["status"] in {"NEED_USER", "OUT_OF_SCOPE"} else "FAILED"
                return self._terminal(task_id, route, status, output["status"], guard, current_agent, current_role, step_number)
            request = output.get("handoff_request")
            if request is None:
                if tuple(guard.agent_history) != route:
                    return self._terminal(task_id, route, "NEED_USER", "incomplete call graph", guard, current_agent, current_role, step_number)
                return self._terminal(task_id, route, "DONE", None, guard, current_agent, current_role, step_number)

            missing_skills = validate_required_skills(request["required_skills"], self.skills_root)
            if missing_skills:
                return self._terminal(task_id, route, "FAILED", "required skill is missing", guard, current_agent, current_role, step_number)
            allowed, reason = guard.record_handoff(request["target_agent"], request["reason_code"])
            if not allowed:
                return self._terminal(task_id, route, reason or "FAILED", "loop limit reached", guard, current_agent, current_role, step_number)
            valid, reason = validate_call_graph_prefix(guard.agent_history)
            if not valid:
                return self._terminal(task_id, route, reason or "NEED_USER", "undefined call graph", guard, current_agent, current_role, step_number)
            next_agent = request["target_agent"]
            if len(guard.agent_history) > len(route) or route[len(guard.agent_history) - 1] != next_agent:
                return self._terminal(task_id, route, "NEED_USER", "handoff does not match selected route", guard, current_agent, current_role, step_number)
            emit_handoff(current_agent, next_agent, request["reason_code"])
            previous_agent = current_agent
            current_agent = next_agent
            current_role = _ROLE_FOR_REASON[request["reason_code"]]
            step_number += 1
            previous_step = step_id
            next_description = request.get("scope") or None
            state = self._state(task_id, route, "RUNNING", current_agent, current_role, step_number, guard)
            self.store.write_state(task_id, state)
            emit_handoff_received(previous_agent, current_agent, current_role)

    def _terminal(self, task_id: str, route: tuple[str, ...], status: str, reason: str | None,
                  guard: LoopGuard | None = None, agent: str | None = None, role: str | None = None,
                  step_number: int = 0) -> dict[str, Any]:
        if status == "DONE": emit_done()
        elif status == "NEED_USER": emit_need_user(reason or "user decision required")
        else: emit_failed(reason or "supervisor failure")
        state = self._state(task_id, route, status, agent, role, step_number, guard)
        self.store.write_state(task_id, state)
        return state

    @staticmethod
    def _state(task_id: str, route: tuple[str, ...], status: str, agent: str | None, role: str | None,
               step_number: int, guard: LoopGuard | None = None, model_tier: str | None = None) -> dict[str, Any]:
        guard = guard or LoopGuard(current_agent=agent)
        state = {"task_id": task_id, "status": status, "route": "_".join(route),
                 "current_agent": agent, "current_role": role, "step_number": step_number,
                 "agent_history": list(guard.agent_history), "agent_handoffs": guard.agent_handoffs,
                 "cross_agent_roundtrips": guard.cross_agent_roundtrips,
                 "same_reason_count": dict(guard.same_reason_count)}
        if model_tier is not None:
            state["model_tier"] = model_tier
        return state

def _dry_run_adapters(route: tuple[str, ...]) -> dict[str, FakeAdapter]:
    responses: dict[str, list[dict[str, Any]]] = {"CLAUDE": [], "CODEX": []}
    if route == DIRECT_CODEX: responses["CODEX"] = [{"status": "OK", "step_id": "01-exec"}]
    elif route == CLAUDE_ONLY: responses["CLAUDE"] = [{"status": "OK", "step_id": "01-plan"}]
    elif route == CLAUDE_CODEX:
        responses["CLAUDE"] = [{"status": "OK", "step_id": "01-plan", "handoff_request": {"target_agent": "CODEX", "reason_code": "IMPLEMENTATION_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}}]
        responses["CODEX"] = [{"status": "OK", "step_id": "02-exec"}]
    elif route == CLAUDE_CODEX_REVIEW:
        responses["CLAUDE"] = [
            {"status": "OK", "step_id": "01-plan", "handoff_request": {"target_agent": "CODEX", "reason_code": "IMPLEMENTATION_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}},
            {"status": "OK", "step_id": "03-review"},
        ]
        responses["CODEX"] = [{"status": "OK", "step_id": "02-exec", "handoff_request": {"target_agent": "CLAUDE", "reason_code": "RISK_REVIEW_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}}]
    elif route == EXCEPTION_GRAPH:
        responses["CLAUDE"] = [
            {"status": "OK", "step_id": "01-plan", "handoff_request": {"target_agent": "CODEX", "reason_code": "IMPLEMENTATION_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}},
            {"status": "OK", "step_id": "03-review", "handoff_request": {"target_agent": "CODEX", "reason_code": "DEBUG_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}},
            {"status": "OK", "step_id": "05-plan"},
        ]
        responses["CODEX"] = [
            {"status": "OK", "step_id": "02-exec", "handoff_request": {"target_agent": "CLAUDE", "reason_code": "RISK_REVIEW_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}},
            {"status": "OK", "step_id": "04-exec", "handoff_request": {"target_agent": "CLAUDE", "reason_code": "DESIGN_REQUIRED", "required_files": [], "required_skills": [], "return_to_agent": "NONE"}},
        ]
    else: raise ValueError("unsupported dry-run route")
    return {agent: FakeAdapter(answer) for agent, answer in responses.items()}

def _real_cli_adapters(timeout_seconds: float, cwd: str | None,
                       on_line: Callable[[str, str], None] | None = None,
                       show_progress: bool = True,
                       timing_log: TimingLog | None = None) -> dict[str, AgentAdapter]:
    from .claude_adapter import ClaudeCLIAdapter
    from .codex_adapter import CodexCLIAdapter

    runner = Runner(WriterLock())
    return {
        "CLAUDE": ClaudeCLIAdapter(runner, timeout_seconds=timeout_seconds, cwd=cwd,
                                   on_line=(lambda line: on_line("CLAUDE", line)) if on_line else None,
                                   show_progress=show_progress, timing_log=timing_log),
        "CODEX": CodexCLIAdapter(runner, timeout_seconds=timeout_seconds, cwd=cwd,
                                  on_line=(lambda line: on_line("CODEX", line)) if on_line else None,
                                  show_progress=show_progress, timing_log=timing_log),
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Supervisor CLI integration")
    routes = ("DIRECT_CODEX", "CLAUDE_ONLY", "CLAUDE_CODEX", "CLAUDE_CODEX_REVIEW", "EXCEPTION_GRAPH")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", choices=routes)
    mode.add_argument("--real-cli", choices=routes)
    parser.add_argument("--task-id")
    parser.add_argument("--runtime-root", default="workspace/runtime")
    parser.add_argument("--skills-root", default="skills")
    parser.add_argument("--cwd")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--task-description")
    parser.add_argument("--no-progress", action="store_true", help="Don't show the 'step running' progress indicator.")
    args = parser.parse_args()
    selected_route = args.real_cli or args.dry_run
    route = {"DIRECT_CODEX": DIRECT_CODEX, "CLAUDE_ONLY": CLAUDE_ONLY, "CLAUDE_CODEX": CLAUDE_CODEX, "CLAUDE_CODEX_REVIEW": CLAUDE_CODEX_REVIEW, "EXCEPTION_GRAPH": EXCEPTION_GRAPH}[selected_route]
    task_id = args.task_id or ("realcli-" if args.real_cli else "dryrun-") + selected_route.lower().replace("_", "-")
    adapters = _real_cli_adapters(args.timeout_seconds, args.cwd, show_progress=not args.no_progress) if args.real_cli else _dry_run_adapters(route)
    state = Supervisor(args.runtime_root, args.skills_root, adapters, initial_description=args.task_description).run(task_id, route)
    return 0 if state["status"] == "DONE" else 1

if __name__ == "__main__":
    raise SystemExit(main())
