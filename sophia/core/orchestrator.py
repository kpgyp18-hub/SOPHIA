from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sophia.brains.ollama import BrainRegistry, BrainResult
from sophia.core.router import Router
from sophia.core.resource_manager import ResourceManager
from sophia.core.context_manager import ContextManager
from sophia.core.call_graph import CallGraph
from sophia.core.health import HealthManager
from sophia.core.invocation_guard import InvocationGuard, RecursiveInvocationError
from sophia.core.provider_resolver import ProviderResolver
from sophia.core.circuit_breaker import CircuitBreaker
from sophia.core.state_machine import TaskStateMachine
from sophia.runtime.task import TaskStatus
from sophia.runtime.message import AgentMessage
from sophia.runtime.store import TaskStore
from sophia.runtime.memory import Embedder, ProjectMemory
from sophia.runtime.budget import ContextBudget
from sophia.runtime.file_changes import apply_file_change, is_file_followup, requested_path
from sophia.runtime.permission import ApprovalCallback
from sophia.runtime.tools import ToolGateway
from sophia.runtime.tool_registry import ToolRegistry
from sophia.runtime.interfaces import VisionAdapter
from sophia.supervisors.supervisors import Aide, Blanc
from sophia.database import DatabaseManager
from sophia.context import ContextCompressor
from sophia.decisions import DecisionManager, QwenFastDecisionProvider, RuleDecisionProvider
from sophia.skills import SkillExecutor, SkillRegistry
from sophia.workers import WorkerPool
from sophia.workspace.manager import WorkspaceManager
from sophia.workers import WorkerCall, WorkerOperation, WorkerStatus
from sophia.judge import JudgeLayer, JudgeVerdict, RuleJudge
from sophia.security import InjectionDetector, SecretRedactor, TrustLabel, isolate_context
from sophia.runtime.cancellation import CancellationRegistry, TaskCancelledError
from sophia.arbitration import NeuralArbitrator
from sophia.core.adaptive import AdaptiveScoreStore, FailureMemory
from sophia.runtime.maintenance import AutonomousMaintenance
from sophia.runtime.retrieval import RetrievalRouter
from sophia.skills import CatalogAdapter, CatalogLoader, SkillCandidateLearner
from sophia.goals import GoalManager, PlanManager, TaskQueue


class SophiaCore:
    max_retry = 2
    def __init__(self, registry: BrainRegistry, telemetry_path: str | Path = "workspace/sophia/telemetry.jsonl",
                 task_store: TaskStore | None = None, context_manager: ContextManager | None = None,
                 approval: ApprovalCallback | None = None, database: DatabaseManager | None = None,
                 resource_manager_config: dict[str, float] | None = None,
                 judge: JudgeLayer | None = None, embedder: Embedder | None = None,
                 arbitrator: NeuralArbitrator | None = None) -> None:
        self.registry = registry
        self.failure_memory = FailureMemory(Path(telemetry_path).with_name("failure-memory.jsonl"))
        self.router = Router(failure_lookup=lambda task: self.failure_memory.count(task_type=task.task_type.value))
        self.resources = ResourceManager(registry, **(resource_manager_config or {}))
        self.decision_manager = DecisionManager(
            QwenFastDecisionProvider(registry.get("fast")) if registry.has("fast") else None,
            RuleDecisionProvider(),
        )
        self.provider_resolver = ProviderResolver(registry)
        self.circuit_breaker = CircuitBreaker()
        self.secret_redactor = SecretRedactor()
        self.injection_detector = InjectionDetector()
        self.invocation_guard = InvocationGuard()
        self.call_graph = CallGraph()
        self.state_machine = TaskStateMachine()
        self.supervisors = {"blanc": Blanc(), "aide": Aide()}
        self.telemetry_path = Path(telemetry_path)
        self.task_store = task_store or TaskStore()
        self.database = database or DatabaseManager(root=self.telemetry_path.parent)
        self.goals = GoalManager(self.database)
        self.plans = PlanManager(self.database)
        self.goal_queue = TaskQueue(self.plans)
        self.session_id = f"session-{uuid4().hex}"
        self.context_compressor = ContextCompressor(self.database)
        self.context_manager = context_manager or ContextManager(
            self.telemetry_path.with_name("context.json"), compressor=self.context_compressor,
            session_id=self.session_id,
        )
        self.memory = ProjectMemory(self.telemetry_path.with_name("memory.db"), embedder=embedder)
        self.context_budget = ContextBudget()
        self.tools = ToolGateway(approval=approval)
        self.tool_registry = ToolRegistry(self.tools)
        self.workspace = WorkspaceManager(self.tools.workspace)
        self.judge = judge or RuleJudge()
        self.vision = VisionAdapter()
        self.skill_registry = SkillRegistry(self.database, library=self.telemetry_path.parent / "skills")
        self.skill_executor = SkillExecutor(self.skill_registry)
        self.skill_learner = SkillCandidateLearner(self.skill_registry)
        self.catalog_loader = CatalogLoader()
        self.catalog_adapter = CatalogAdapter(self.skill_registry, self.tool_registry)
        self.retrieval_router = RetrievalRouter()
        self.adaptive_scores = AdaptiveScoreStore()
        self.arbitrator = arbitrator
        self._active_skill_by_task: dict[str, str] = {}
        self.worker_pool = WorkerPool(tool_registry=self.tool_registry)
        self.worker_pool.register_handler("memory-search", self._memory_search_worker)
        self.health = HealthManager(registry, self.database, self.tools.workspace,
                                    self.tool_registry, self.worker_pool)
        self.maintenance = AutonomousMaintenance(self.memory, self.skill_registry)
        self.runtime_events: dict[str, list[dict[str, object]]] = {}
        self.cancellations = CancellationRegistry()

    def cancel_task(self, task_id: str) -> bool:
        """Request cooperative cancellation of a currently running task."""
        return self.cancellations.request(task_id)

    def register_skill_handler(self, identifier: str, handler) -> None:
        """Register a host-owned skill handler; persisted procedures remain non-executable."""
        self.skill_executor.register_handler(identifier, handler)

    def _memory_search_worker(self, call: WorkerCall) -> list[str]:
        return self.memory.search(str(call.payload))

    def _run_memory_worker(self, task, query: str) -> list[str]:
        result = self.worker_pool.run(WorkerCall("memory-search", task.id, WorkerOperation.SEARCH, query,
                                                 visited_providers=self.invocation_guard.visited_providers,
                                                 visited_workers=self.invocation_guard.visited_workers,
                                                 cancellation=self.cancellations.get(task.id)))
        event = {"kind": "worker", "worker_id": result.worker_id, "call_id": result.call_id,
                 "status": result.status.value, "attempts": result.attempts}
        self.runtime_events.setdefault(task.id, []).append(event)
        return list(result.value) if result.status is WorkerStatus.COMPLETED and isinstance(result.value, list) else []

    def _run_requested_skill(self, task, user_input: str) -> str | None:
        """Execute only an explicit `skill:<identifier>` request through a registered host handler."""
        marker = "skill:"
        if marker not in user_input.lower():
            return None
        identifier = user_input[user_input.lower().index(marker) + len(marker):].strip().split()[0] if user_input[user_input.lower().index(marker) + len(marker):].strip() else ""
        if not identifier:
            return "Skill request ignored: no identifier supplied."
        execution = self.skill_executor.execute(identifier, {
            "task_id": task.id, "task_type": task.task_type.value, "input": user_input,
        })
        self.runtime_events.setdefault(task.id, []).append({"kind": "skill", "skill_id": execution.skill_id,
                                                             "status": execution.status,
                                                             "duration_ms": execution.duration_ms})
        self._active_skill_by_task[task.id] = execution.skill_id
        if execution.status != "completed":
            return f"Skill {identifier}: {execution.status} ({execution.error})"
        return f"Skill {identifier} result: {execution.result}"

    def inspect_image(self, image: str | Path, output: str | Path | None = None) -> dict[str, object]:
        """Run the local-only vision path and persist its analysis for verification."""
        source = Path(image)
        target = self.tools.resolve(output) if output else self.telemetry_path.with_name(f"vision-{source.stem}.json")
        return self.vision.inspect_to_file(source, target)

    def delete_file(self, path: str | Path) -> None:
        """Perform an explicit LEVEL 3 file deletion through the permission layer."""
        self.tool_registry.dispatch(
            "filesystem.delete", "aide",
            lambda invocation: self.tools.delete(
                path, approval_checked=bool(invocation.permission and invocation.permission.user_approval_granted),
            ),
            request_context=f"delete {path}",
        )

    def run_shell(self, command: str | list[str]) -> str:
        """Run an explicit local command through the fixed CLI allowlist and safety layer."""
        return self.tool_registry.dispatch(
            "shell", "aide",
            lambda _invocation: self.tools.shell(
                command, allowed={"python", "py", "pytest", "git", "pip", "npm", "winget"},
            ),
            request_context=" ".join(command) if isinstance(command, list) else command,
        )

    def _run_project_tests(self) -> tuple[bool | None, str]:
        """Run the fixed local test command only when this workspace has a test suite.

        ``None`` means that no suite is configured, not that the change passed.
        The command still goes through ToolGateway, so it retains its allowlist,
        risk classification, and audit record.
        """
        if not self.tools.resolve("tests").is_dir():
            return None, "No tests/ directory configured; test step skipped."
        outcome = self.tool_registry.dispatch(
            "shell", "aide",
            lambda _invocation: self.tools.shell_result(
                ["python", "-m", "pytest", "tests", "-q"], allowed={"python"},
            ),
            request_context="python -m pytest tests -q",
        )
        if outcome.returncode == 0:
            return True, outcome.output.strip() or "pytest passed"
        return False, outcome.output.strip() or f"pytest exited with code {outcome.returncode}"

    def _final_review(self, task, brain_name: str, context: str, aide_result: BrainResult,
                      handoff: AgentMessage) -> BrainResult:
        """Complete the v0.3 coding path: Aide artifact -> test -> Blanc review."""
        review_brain = "reasoning" if self.registry.has("reasoning") else "general"
        review_context = self.context_budget.fit(
            context + "\n\nAide implementation artifact:\n" + aide_result.text
            + "\n\nStructured Aide handoff:\n" + json.dumps(handoff.as_dict(), ensure_ascii=False)
            + "\n\nReview the concrete implementation and test evidence. State whether it is ready."
        )
        review = self._run_supervisor(task, "blanc", review_brain, review_context)
        return BrainResult(
            f"[Aide] {handoff.summary}\n[Test] {handoff.metadata['test_summary']}\n\n[Blanc] {review.text}",
            review.prompt_tokens, review.output_tokens, review.duration_ns, review.estimated_cost,
            model=review.model, provider=review.provider,
        )

    def _run_supervisor(self, task, supervisor: str, brain_name: str, context: str) -> BrainResult:
        """Make bounded local inference attempts and expose exhaustion as TASK_PAUSED."""
        last_error: OSError | None = None
        for attempt in range(self.max_retry + 1):
            try:
                task.brain_calls += 1
                brain = self.resources.acquire(brain_name)
                self.resources.mark_busy(brain_name)
                provider = str(getattr(brain, "provider", "ollama"))
                self.circuit_breaker.allow(provider)
                outbound = self.secret_redactor.redact(context)
                if outbound.redacted:
                    self.runtime_events.setdefault(task.id, []).append({
                        "kind": "secret_redaction", "direction": "provider_input", "provider": provider,
                        "kinds": outbound.kinds, "status": "SECRET_REDACTED",
                    })
                call_id = self.invocation_guard.enter(provider, worker=supervisor, root_task_id=task.id)
                self.call_graph.start(call_id, task_id=task.id, supervisor=supervisor,
                                      provider=provider, brain=brain_name)
                try:
                    safe_input = self.secret_redactor.redact(task.input)
                    if safe_input.redacted:
                        self.runtime_events.setdefault(task.id, []).append({
                            "kind": "secret_redaction", "direction": "provider_input", "provider": provider,
                            "kinds": safe_input.kinds, "status": "SECRET_REDACTED",
                        })
                    safe_task = replace(task, input=safe_input.text)
                    result = self.supervisors[supervisor].run(safe_task, brain, outbound.text)
                    self.call_graph.finish(call_id, status="completed")
                    self.resources.mark_ready(brain_name)
                    self.circuit_breaker.success(provider)
                    return result
                except (ConnectionError, TimeoutError, OSError):
                    self.circuit_breaker.failure(provider)
                    self.call_graph.finish(call_id, status="failed")
                    self.resources.mark_error(brain_name)
                    raise
                except Exception:
                    self.call_graph.finish(call_id, status="failed")
                    self.resources.mark_error(brain_name)
                    raise
                finally:
                    self.invocation_guard.leave(provider, worker=supervisor)
                    # Reasoning/coder roles may require a costly model swap.  Once
                    # their turn ends, restore the interactive General role so the
                    # next ordinary request begins from the configured baseline.
                    if brain_name != "general" and self.registry.has("general"):
                        try:
                            self.resources.restore_general()
                        except (ConnectionError, OSError, TimeoutError):
                            # The failed acquire clears active_name/model itself;
                            # never retain a phantom resource after restoration.
                            pass
            except (ConnectionError, TimeoutError, OSError) as error:
                last_error = error
                if attempt < self.max_retry:
                    task.retry_count += 1
                    context += "\nRetry: produce a concise recoverable result."
        assert last_error is not None
        raise RuntimeError(f"brain unavailable after {self.max_retry} retries: {last_error}") from last_error

    def _provider_health(self) -> dict[str, bool]:
        """Build resolver input by provider, not by logical brain-role aliases."""
        providers: dict[str, bool] = {}
        for name in self.registry.names():
            provider = str(getattr(self.registry.get(name), "provider", "ollama"))
            providers[provider] = True
        return providers

    def _record_injection(self, task_id: str, text: str, *, source_type: str, source_id: str = "",
                          chunk_id: str = "") -> None:
        """Persist detection metadata only; never persist untrusted source text."""
        detected_pattern = self.injection_detector.detected_pattern(text)
        if detected_pattern is None:
            return
        self.runtime_events.setdefault(task_id, []).append({
            "kind": "injection", "source_type": source_type, "source_id": source_id,
            "chunk_id": chunk_id or source_id or "whole_input",
            "rule": "local_pattern", "detected_pattern": detected_pattern,
            "severity": "high", "action": "isolated",
            "task_id": task_id, "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def run(self, user_input: str, *, brain_override: str | None = None) -> tuple[object, BrainResult]:
        explicit_path = requested_path(user_input)
        prior_coding_task = self.context_manager.last_task_type() in {"coding", "debug"}
        continuation_requested = explicit_path is None and (is_file_followup(user_input) or prior_coding_task)
        target_path = explicit_path or (self.context_manager.last_file_target() if continuation_requested else None)
        decision_outcome = self.decision_manager.decide(user_input)
        decision = self.router.decide(user_input, coding_continuation=continuation_requested, hint=decision_outcome)
        task, supervisor, brain_name = decision.task, decision.supervisor, decision.brain
        arbitration_state: dict[str, float] = {}
        if self.arbitrator is not None and self.arbitrator.enabled:
            failure_count = self.failure_memory.count(task_type=task.task_type.value, brain=brain_name)
            arbitration_state["state.provider_failure"] = min(1.0, failure_count / 3)
            usage = self.resources.usage()
            if usage.vram_total_mib and usage.vram_free_mib is not None:
                arbitration_state["state.low_vram"] = 1.0 - usage.vram_free_mib / usage.vram_total_mib
            try:
                arbitration_graph = getattr(self.arbitrator.backend, "graph", None)
                for skill in self.skill_registry.list(status="active"):
                    node_id = f"skill.{skill.name}"
                    if skill.quality_samples and arbitration_graph is not None and node_id in arbitration_graph.nodes:
                        arbitration_state[node_id] = min(0.15, skill.quality_score * 0.15)
            except RuntimeError:
                pass
        arbitration_result = self.arbitrator.recommend(
            task, user_input, state=arbitration_state, actual_route=supervisor,
        ) if self.arbitrator is not None else None
        self._record_injection(task.id, user_input, source_type="user_input", source_id=task.id)
        cancellation = self.cancellations.create(task.id)
        self.runtime_events.setdefault(task.id, []).append({
            "kind": "decision", "provider": decision_outcome.provider,
            "confidence": decision_outcome.confidence, "review_required": decision_outcome.review_required,
            "crosscheck_required": decision_outcome.crosscheck_required,
        })
        if arbitration_result is not None:
            self.runtime_events.setdefault(task.id, []).append({
                "kind": "arbitration", "mode": arbitration_result.mode.value,
                "operation": self.arbitrator.operation, "winners": list(arbitration_result.winners),
                "supervisor_hint_only": True,
            })
        self.state_machine.transition(task, TaskStatus.ROUTED)
        if brain_override is not None:
            if not self.registry.has(brain_override):
                available = ", ".join(self.registry.names())
                raise ValueError(f"unknown brain '{brain_override}' (available: {available})")
            brain_name = brain_override
        elif decision.context_class == "complex" and self.registry.has("reasoning"):
            brain_name = "reasoning"
        adaptive_scores = self.adaptive_scores.scores()
        if self.arbitrator is not None:
            for name, bias in self.arbitrator.provider_biases(arbitration_result).items():
                adaptive_scores[name] = adaptive_scores.get(name, 0.0) + bias
        selection = self.provider_resolver.resolve(
            task, brain_name, route=decision.escalation.route,
            health=self._provider_health(),
            privacy_required=not (brain_override is not None and bool(
                getattr(self.registry.get(brain_override), "external", False)
            )),
            visited_providers=self.invocation_guard.visited_providers,
            resource_available=self.resources.can_schedule,
            adaptive_scores=adaptive_scores or None,
        )
        if selection.requires_user_approval:
            self.state_machine.transition(task, TaskStatus.USER_DECISION_REQUIRED)
            result = BrainResult(f"USER_DECISION_REQUIRED: {selection.reason}")
            self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, [])
            return task, result
        brain_name = selection.brain
        self.state_machine.transition(task, TaskStatus.PLANNED)
        self.task_store.write(task.id, status=task.status, supervisor=supervisor, brain=brain_name,
                              context_class=decision.context_class, escalation=decision.escalation.as_dict(), handoff=None)
        try:
            self.database.save_session(self.session_id, status="active", supervisor=supervisor, brain=brain_name)
            self.database.save_task(task.id, session_id=self.session_id, status=task.status.value,
                                    task_type=task.task_type.value, supervisor=supervisor, brain=brain_name,
                                    complexity=task.complexity)
        except RuntimeError:
            pass
        prior_context = self.context_manager.build()
        context = (
            f"Task type: {task.task_type}; complexity: {task.complexity}; "
            f"context class: {decision.context_class}; context budget: 8192 tokens."
        )
        if prior_context:
            context += "\n\n" + prior_context
        memories = self._run_memory_worker(task, user_input)
        if memories:
            self._record_injection(task.id, "\n".join(memories), source_type="memory", source_id="retrieval")
            context += "\n\n" + isolate_context("\n".join(memories), TrustLabel.TRUSTED_MEMORY)
        skill_context = self._run_requested_skill(task, user_input)
        if skill_context:
            self._record_injection(task.id, skill_context, source_type="tool_output", source_id="skill")
            context += "\n\n" + isolate_context(self.secret_redactor.redact(skill_context).text, TrustLabel.TOOL_OUTPUT)
        self.state_machine.transition(task, TaskStatus.EXECUTING)
        try:
            cancellation.raise_if_cancelled()
            result = self._run_supervisor(task, supervisor, brain_name, self.context_budget.fit(context))
            cancellation.raise_if_cancelled()
        except TaskCancelledError as error:
            self.state_machine.transition(task, TaskStatus.CANCEL_REQUESTED)
            self.state_machine.transition(task, TaskStatus.CANCELLING)
            self.state_machine.transition(task, TaskStatus.CANCELLED)
            result = BrainResult(str(error))
            self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, [])
            return task, result
        except RecursiveInvocationError as error:
            self.state_machine.transition(task, TaskStatus.RECURSION_BLOCKED)
            result = BrainResult(f"RECURSION_BLOCKED: {error}")
            self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, [])
            return task, result
        except RuntimeError as error:
            self.failure_memory.record(task_type=task.task_type.value, brain=brain_name, reason=str(error),
                                       skill_id=self._active_skill_by_task.get(task.id))
            self.state_machine.transition(task, TaskStatus.PAUSED)
            result = BrainResult(f"TASK_PAUSED: {error}")
            self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, [])
            return task, result
        if decision.escalation.route.value == "dual_review" and decision.escalation.next_supervisor:
            reviewer = decision.escalation.next_supervisor
            review_context = self.context_budget.fit(context + "\n\nDraft answer for review:\n" + result.text)
            task.review_count += 1
            self.state_machine.transition(task, TaskStatus.REVIEWING)
            try:
                result = self._run_supervisor(task, reviewer, brain_name, review_context)
            except RuntimeError as error:
                self.state_machine.transition(task, TaskStatus.PAUSED)
                result = BrainResult(f"TASK_PAUSED: {error}")
                self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, [])
                return task, result
        file_error: str | None = None
        workflow_messages: list[AgentMessage] = []
        completed_change: str | None = None
        test_passed: bool | None = None
        test_summary: str | None = None
        checkpoint = None
        if task.task_type.value in {"coding", "debug"} and (target_path or continuation_requested):
            if target_path is None:
                file_error = "cannot determine target file; name a workspace file before asking to continue an edit"
            else:
                try:
                    checkpoint = self.workspace.checkpoint_file(target_path)
                    self.workspace.assert_fresh(checkpoint)
                    cancellation.raise_if_cancelled()
                    change = self.tool_registry.dispatch(
                        "filesystem.write", "aide",
                        lambda _invocation: apply_file_change(
                            self.tools, user_input, result.text, target_path=target_path,
                        ),
                        request_context=user_input,
                        task_id=task.id, idempotency_key=f"{task.id}:filesystem.write",
                        cancellation=cancellation,
                    )
                    if change is None:
                        file_error = "no complete fenced file artifact was produced; no file was changed"
                        # A checkpoint can be created before the model artifact is
                        # parsed, but it must never be left active when no write
                        # follows.  This keeps checkpoint state an accurate audit
                        # of an actual modification attempt.
                        self.workspace.rollback(checkpoint)
                        checkpoint = None
                    else:
                        completed_change = f"Verified write: {change.path}"
                        task.tool_calls += 1
                        test_passed, test_summary = self._run_project_tests()
                        self.state_machine.transition(task, TaskStatus.VERIFYING)
                        task.tool_calls += 1 if test_passed is not None else 0
                        # The immutable tool contract, not an incidental caller
                        # convention, determines whether this write may commit.
                        # The default filesystem.write capability is deliberately
                        # checkpointed; retain fail-closed behavior if a future
                        # spec changes the policy unexpectedly.
                        write_spec = self.tool_registry.get("filesystem.write")
                        if not write_spec.requires_checkpoint:
                            self.workspace.rollback(checkpoint)
                            checkpoint = None
                            raise PermissionError("filesystem.write must require a checkpoint before commit")
                        judgement = self.judge.validate_test_claim(
                            passed=test_passed, summary=test_summary or "", changed_path=change.path,
                        )
                        if not judgement.approved and judgement.verdict is not JudgeVerdict.UNCERTAIN:
                            self.workspace.rollback(checkpoint)
                            checkpoint = None
                            file_error = f"judge rejected commit: {judgement.reason}"
                        else:
                            handoff = AgentMessage(
                                sender="aide", receiver="blanc", task_id=task.id, status="completed",
                                summary=completed_change, request="Review the applied implementation and test result.",
                                metadata={"path": change.path, "test_passed": test_passed, "test_summary": test_summary,
                                          "judge": {"criterion": judgement.criterion, "reason": judgement.reason,
                                                    "verdict": judgement.verdict.value}},
                            )
                            workflow_messages.append(handoff)
                            task.review_count += 1
                            self.state_machine.transition(task, TaskStatus.REVIEWING)
                            result = self._final_review(task, brain_name, context, result, handoff)
                            workflow_messages.append(AgentMessage(
                                sender="blanc", receiver="core", task_id=task.id, status="completed",
                                summary="Final review completed.", metadata={"review": result.text},
                            ))
                            self.workspace.commit(checkpoint)
                            checkpoint = None
                except TaskCancelledError as error:
                    if checkpoint is not None:
                        self.workspace.rollback(checkpoint)
                    self.state_machine.transition(task, TaskStatus.CANCEL_REQUESTED)
                    self.state_machine.transition(task, TaskStatus.CANCELLING)
                    self.state_machine.transition(task, TaskStatus.CANCELLED)
                    result = BrainResult(str(error), result.prompt_tokens, result.output_tokens, result.duration_ns, result.estimated_cost)
                    self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, workflow_messages)
                    return task, result
                except (OSError, PermissionError, ValueError) as error:
                    if checkpoint is not None:
                        try:
                            self.workspace.rollback(checkpoint)
                        except (OSError, PermissionError, ValueError):
                            pass
                    file_error = f"file write failed: {error}"
        if test_passed is False:
            file_error = f"test failed: {test_summary}"
        if file_error:
            if task.status is not TaskStatus.USER_DECISION_REQUIRED:
                self.state_machine.transition(task, TaskStatus.FAILED)
            result = BrainResult(f"FAILED: {file_error}", result.prompt_tokens, result.output_tokens,
                                 result.duration_ns, result.estimated_cost)
        else:
            if task.status is not TaskStatus.REVIEWING:
                self.state_machine.transition(task, TaskStatus.VERIFYING)
            self.state_machine.transition(task, TaskStatus.COMPLETED)
        self._finish(task, supervisor, brain_name, decision, user_input, result, target_path, workflow_messages)
        return task, result

    def _finish(self, task, supervisor: str, brain_name: str, decision, user_input: str,
                result: BrainResult, target_path: str | None, messages: list[AgentMessage]) -> None:
        self.task_store.write(task.id, status=task.status, supervisor=supervisor, brain=brain_name,
                              context_class=decision.context_class, escalation=decision.escalation.as_dict(),
                              handoff=None, messages=[message.as_dict() for message in messages],
                              call_graph=self.call_graph.snapshot(task.id),
                              runtime_events=self.runtime_events.get(task.id, []),
                              counters={"brain_calls": task.brain_calls, "retry_count": task.retry_count,
                                        "review_count": task.review_count, "tool_calls": task.tool_calls},
                              result="completed" if task.status is TaskStatus.COMPLETED else result.text)
        self.context_manager.add(task.id, user_input, result.text, target_path=target_path, task_type=task.task_type.value)
        skill_id = self._active_skill_by_task.pop(task.id, None)
        if skill_id is not None:
            verdict = "PASS" if task.status is TaskStatus.COMPLETED else "FAIL" if task.status is TaskStatus.FAILED else "UNCERTAIN"
            try:
                self.skill_registry.record_outcome(skill_id, verdict, task_id=task.id, reason=result.text[:500])
            except (KeyError, RuntimeError):
                pass
        if task.status is TaskStatus.COMPLETED and task.tool_calls:
            try:
                self.skill_learner.observe_success(task.task_type.value, user_input,
                                                   description="Repeated successful runtime task pattern.")
            except (OSError, RuntimeError, ValueError):
                pass
        if task.status is TaskStatus.FAILED:
            self.failure_memory.record(task_type=task.task_type.value, brain=brain_name, reason=result.text,
                                       skill_id=skill_id)
        if self.arbitrator is not None:
            verdict = "PASS" if task.status is TaskStatus.COMPLETED else "FAIL" if task.status is TaskStatus.FAILED else "UNCERTAIN"
            self.arbitrator.feedback(verdict)
        self.memory.add(task.id, f"User: {user_input}\nSOPHIA: {result.text}", source_type="runtime_task",
                        source_id=task.id, session_id=self.session_id, supervisor=supervisor, brain=brain_name,
                        provider=result.provider or "local", confidence=1.0, trust_level="TOOL_VERIFIED")
        try:
            self.database.save_task(task.id, session_id=self.session_id, status=task.status.value,
                                    task_type=task.task_type.value, supervisor=supervisor, brain=brain_name,
                                    complexity=task.complexity,
                                    metadata={"result": result.text, "counters": {"brain_calls": task.brain_calls,
                                              "retry_count": task.retry_count, "review_count": task.review_count,
                                              "tool_calls": task.tool_calls}})
            self.database.record("task", {"task_id": task.id, "status": task.status.value,
                                           "supervisor": supervisor, "brain": brain_name,
                                           "result": result.text})
        except RuntimeError:
            # Runtime task files remain the durable safety fallback if every DB
            # backend is unavailable; health reports surface the condition.
            pass
        self._record(task.id, supervisor, brain_name, result, retry_count=task.retry_count)

    def _record(self, task_id: str, supervisor: str, brain: str, result: BrainResult, *, retry_count: int = 0) -> None:
        self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        safe_result = self.secret_redactor.redact(result.text)
        event = {"at": datetime.now(timezone.utc).isoformat(), "task_id": task_id, "supervisor": supervisor,
                 "brain_role": brain, "model": result.model or getattr(self.registry.get(brain), "model", brain),
                 "provider": result.provider or "local", "input_tokens": result.prompt_tokens,
                 "output_tokens": result.output_tokens, "duration_ns": result.duration_ns,
                 "duration_ms": result.duration_ns / 1_000_000,
                 "tokens_per_second": (result.output_tokens / (result.duration_ns / 1_000_000_000)
                                       if result.duration_ns else None),
                 "retry_count": retry_count, "estimated_cost": result.estimated_cost,
                 "result_redacted": safe_result.redacted}
        with self.telemetry_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
