"""Bridge live chat turns into the shared S.O.P.H.I.A task store.

This deliberately records routing and handoff state only. It does not invoke
Ollama or alter ChatSession's existing agent execution path.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sophia.core.router import Router
from sophia.core.escalation import EscalationPolicy
from sophia.runtime.store import TaskStore
from sophia.runtime.task import TaskStatus


class ChatTaskTracker:
    def __init__(self, source: str, store: TaskStore | None = None) -> None:
        self.source = source
        self.store = store or TaskStore()
        self.router = Router()
        self.escalation_policy = EscalationPolicy()

    @staticmethod
    def _event(event_type: str, **details: str) -> dict[str, str]:
        return {"type": event_type, "at": datetime.now(timezone.utc).isoformat(), **details}

    def begin(self, message: str) -> str:
        decision = self.router.decide(message)
        task = decision.task
        task.status = TaskStatus.EXECUTING
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
            events=[self._event("received", source=self.source)],
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
        escalation = state.get("escalation", {})
        if escalation.get("route") != "local":
            events.append(self._event("awaiting_handoff", route=str(escalation.get("route", ""))))
            self.store.update(task_id, status=TaskStatus.ESCALATED, events=events)
            return
        events.append(self._event("completed", source=self.source))
        self.store.update(task_id, status=TaskStatus.COMPLETED, events=events)
