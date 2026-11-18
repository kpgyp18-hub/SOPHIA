"""Bridge live chat turns into the shared S.O.P.H.I.A task store.

This deliberately records routing and handoff state only. It does not invoke
Ollama or alter ChatSession's existing agent execution path.

State mapping S3 (output/STATE_MAPPING_DESIGN_20261118.md): status changes go
through Core's TaskStateMachine instead of direct writes. An illegal transition
leaves the stored status unchanged and is recorded as an ``invalid_transition``
event; it never raises into the chat. ESCALATED is not defined in the Core state
machine, so it stays a direct write marked by a ``transition_exception`` event
until Core defines it (sophia/core is frozen).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sophia.core.router import Router
from sophia.core.escalation import EscalationPolicy
from sophia.core.state_machine import InvalidTaskTransition, TaskStateMachine
from sophia.runtime.store import TaskStore
from sophia.runtime.task import TaskStatus

# Legal Core paths used by the chat tracker.
_BEGIN_PATH = (TaskStatus.CLASSIFIED, TaskStatus.ROUTED, TaskStatus.PLANNED, TaskStatus.EXECUTING)
_COMPLETE_PATH = (TaskStatus.VERIFYING, TaskStatus.COMPLETED)
_HOLD_PATH = (TaskStatus.VERIFYING, TaskStatus.USER_DECISION_REQUIRED)


class ChatTaskTracker:
    def __init__(self, source: str, store: TaskStore | None = None, *, core_shadow: object | None = None) -> None:
        self.source = source
        self.store = store or TaskStore()
        self.router = Router()
        self.escalation_policy = EscalationPolicy()
        self.state_machine = TaskStateMachine()
        # Core connection C1: observation only; never consumed by the chat path.
        if core_shadow is None:
            from sophia.runtime.core_shadow import CoreShadow
            core_shadow = CoreShadow()
        self.core_shadow = core_shadow

    def _core_snapshot(self, message: str) -> dict:
        try:
            snapshot = self.core_shadow.snapshot(message)
            return snapshot if isinstance(snapshot, dict) else {"error": "invalid_snapshot"}
        except Exception as exc:  # noqa: BLE001 - observation must not break begin()
            return {"error": type(exc).__name__[:64]}

    def observe_dispatch(self, task_id: str | None, target: str | None, explicit: bool | None) -> None:
        """Record the live dispatch next to the Core shadow decision (C1, observation only)."""
        if not task_id:
            return
        try:
            state = self.store.read(task_id)
            if state is None:
                return
            from sophia.runtime.core_shadow import dispatch_view
            shadow = dict(state.get("core_shadow") or {})
            shadow["dispatch"] = dispatch_view(shadow, target=target, explicit=explicit)
            self.store.update(task_id, core_shadow=shadow)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _event(event_type: str, **details: str) -> dict[str, str]:
        return {"type": event_type, "at": datetime.now(timezone.utc).isoformat(), **details}

    def _walk(self, current: object, path: tuple[TaskStatus, ...]) -> tuple[TaskStatus | None, str | None]:
        """Advance along ``path`` with Core's rules. Returns (final status, error)."""
        try:
            status = TaskStatus(str(current))
        except ValueError:
            return None, f"unknown stored status: {str(current)[:64]}"
        # Resume from the current position when the task is already part-way along the path.
        steps = path[path.index(status) + 1:] if status in path else path
        probe = SimpleNamespace(status=status)
        try:
            for target in steps:
                self.state_machine.transition(probe, target)
        except InvalidTaskTransition as exc:
            return None, str(exc)[:300]
        return probe.status, None

    def begin(self, message: str) -> str:
        decision = self.router.decide(message)
        task = decision.task
        status, error = self._walk(task.status, _BEGIN_PATH)
        events = [self._event("received", source=self.source)]
        if error is not None:
            events.append(self._event("invalid_transition", to=TaskStatus.EXECUTING.value, error=error))
            status = task.status
        task.status = status
        self.store.write(
            task.id,
            source=self.source,
            input=message,
            status=task.status,
            task_type=task.task_type,
            supervisor=decision.supervisor,
            brain=decision.brain,
            context_class=decision.context_class,
            escalation=decision.escalation.as_dict(),
            core_shadow=self._core_snapshot(message),
            events=events,
        )
        return task.id

    def reply(self, task_id: str | None, agent: str, text: str) -> None:
        if not task_id:
            return
        state = self.store.read(task_id)
        if state is None:
            return
        events = list(state.get("events", []))
        events.append(self._event("reply", agent=agent, text=text))
        changes = {"events": events}
        if text.lstrip().startswith("(error:"):
            task = self.router.classify(str(state.get("input", "")))
            escalation = self.escalation_policy.after_failure(task)
            events.append(self._event("escalated", route=str(escalation.route), reason=escalation.reason))
            changes["events"] = events
            changes["escalation"] = escalation.as_dict()
        self.store.update(task_id, **changes)

    def handoff(self, task_id: str | None, from_agent: str, to_agent: str, hop: int) -> None:
        """Record an explicit agent-to-agent relay in the durable task state."""
        if not task_id:
            return
        state = self.store.read(task_id)
        if state is None:
            return
        handoff = {"from": from_agent, "to": to_agent, "hop": hop}
        events = list(state.get("events", []))
        events.append(self._event("handoff", **{key: str(value) for key, value in handoff.items()}))
        self.store.update(task_id, handoff=handoff, events=events)

    def complete(self, task_id: str | None) -> None:
        if not task_id:
            return
        state = self.store.read(task_id)
        if state is None:
            return
        events = list(state.get("events", []))
        if events and events[-1].get("type") in ("completed", "awaiting_handoff"):
            return  # Restart result collection must not append completion twice.
        escalation = state.get("escalation", {})
        if escalation.get("route") != "local":
            # Not representable in Core's state machine; kept as before and marked.
            events.append(self._event("awaiting_handoff", route=str(escalation.get("route", ""))))
            events.append(self._event("transition_exception", to=TaskStatus.ESCALATED.value,
                                      reason="ESCALATED is not defined in sophia.core.state_machine"))
            self.store.update(task_id, status=TaskStatus.ESCALATED, events=events)
            return
        status, error = self._walk(state.get("status"), _COMPLETE_PATH)
        if error is not None:
            events.append(self._event("invalid_transition", to=TaskStatus.COMPLETED.value, error=error))
            self.store.update(task_id, events=events)
            return
        events.append(self._event("completed", source=self.source))
        self.store.update(task_id, status=status, events=events)

    def hold(self, task_id: str | None, reason: str) -> None:
        """Observe an execution hold; the journal remains its admission authority."""
        if not task_id:
            return
        state = self.store.read(task_id)
        if state is None:
            return
        events = list(state.get("events", []))
        if events and events[-1].get("type") == "execution_hold" and events[-1].get("reason") == reason:
            return
        events.append(self._event("execution_hold", reason=reason))
        if state.get("status") == TaskStatus.USER_DECISION_REQUIRED.value:
            self.store.update(task_id, events=events)  # already held: record the new reason only
            return
        status, error = self._walk(state.get("status"), _HOLD_PATH)
        if error is not None:
            events.append(self._event("invalid_transition", to=TaskStatus.USER_DECISION_REQUIRED.value, error=error))
            self.store.update(task_id, events=events)
            return
        self.store.update(task_id, status=status, events=events)
