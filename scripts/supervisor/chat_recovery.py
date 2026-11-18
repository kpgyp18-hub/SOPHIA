"""Opt-in durable admission for live chat sends; Runner still owns processes.

One journal key per cwd prevents a new provider or a changed task id from
bypassing unresolved work after a host restart. A record describes a logical
send, including its existing bounded resume retries, not each subprocess.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sophia.runtime.execution_contract import ExecutionContract
from sophia.runtime.execution_recovery import ExecutionJournal, ReconciliationRequired, settle_attempt

from .long_task import pid_alive
from .effect_evidence import compare_fingerprints, unknown_fingerprint, workspace_fingerprint

if TYPE_CHECKING:
    from .chat_adapter import ChatAdapter

CHAT_RECOVERY_KEY = "live-chat"
_MESSAGE_EXECUTION: ContextVar = ContextVar("sophia_message_execution", default=None)
# State mapping S2: the tracker task this send belongs to, recorded on the attempt.
_TASK_REF: ContextVar = ContextVar("sophia_chat_task_ref", default=None)
_TASK_REF_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


@contextmanager
def task_ref_scope(task_ref: str | None):
    """Bind a tracker task id to sends made inside this block (observation only)."""
    token = _TASK_REF.set(task_ref if isinstance(task_ref, str) and _TASK_REF_PATTERN.fullmatch(task_ref) else None)
    try:
        yield
    finally:
        _TASK_REF.reset(token)


@dataclass
class MessageExecution:
    journal: ExecutionJournal
    contract: ExecutionContract
    cwd: Path
    unsettled: bool = False


@contextmanager
def message_execution(journal: ExecutionJournal, contract: ExecutionContract, cwd: Path):
    value = MessageExecution(journal, contract, cwd.resolve())
    token = _MESSAGE_EXECUTION.set(value)
    try:
        yield value
    finally:
        _MESSAGE_EXECUTION.reset(token)


# The pre-send probe runs inside the provider's own deadline, so it may spend at
# most 0.5s and at most 1% of what remains (measured ~120ms). A skipped probe
# only makes effects "unknown"; it never changes admission or the deadline.
_PRE_SEND_PROBE_MAX_S = 0.5
_PRE_SEND_PROBE_SHARE = 0.01


def _fingerprint(adapter, budget, *, after=False):
    started = time.monotonic()
    try:
        timeout = 2.0 if after else min(_PRE_SEND_PROBE_MAX_S, budget.remaining_s() * _PRE_SEND_PROBE_SHARE)
        if timeout < 0.1:
            snapshot = unknown_fingerprint("insufficient_observation_budget")
        else:
            snapshot = workspace_fingerprint(adapter.cwd, timeout_s=timeout)
    except Exception:
        snapshot = unknown_fingerprint("observation_failed")
    snapshot["observation_s"] = round(time.monotonic() - started, 6)
    return snapshot


def _observation(adapter, before, after, outcome, events, detail, failure, returned, reply):
    # These axes describe evidence. settle_attempt remains the admission rule.
    if detail == "not_started":
        process = "not_started"
    elif "cleanup_incomplete" not in events and (outcome in ("completed", "nonzero_exit")
                                                or "child_exit" in events or "cleanup_end" in events):
        process = "exited"
    else:
        process = "unverified"
    report = adapter.last_completion_report
    if process == "not_started":
        result = "none"
    elif outcome == "nonzero_exit":
        result = "error"
    elif report is not None and report.final_event_seen:
        result = "final"
    elif report is not None and report.output_lines:
        result = "partial"
    elif returned:
        result = "final" if reply else "none"
    elif getattr(failure, "output", None):
        result = "partial"
    else:
        result = "error" if failure else "none"
    try:
        effects, evidence = compare_fingerprints(before, after)
    except Exception:
        effects = "unknown"
        evidence = {"scope": unknown_fingerprint("invalid_snapshot")["scope"], "observation_error": True}
    try:
        observed_capability = adapter.effect_capability
    except Exception:
        observed_capability = "unknown"
    if observed_capability not in ("read_only", "workspace_write", "workspace_write_network", "unknown"):
        observed_capability = "unknown"
    evidence.update(capability=observed_capability,
                    capability_scope="Adapter flags only; global CLI policy, hooks and extra tools are not inspected. "
                                     "Not a grant or isolation proof.")
    return {"process": process, "result": result, "persisted": False, "effects": effects}, evidence


def recoverable_send(adapter: ChatAdapter, task_id: str, prompt: str, *, context: str | None = None,
                     execution_contract: ExecutionContract | None = None) -> str:
    from .chat_adapter import ChatAdapterError, ChatExecutionBlocked

    # Admission can fail before _send resets per-call state. Do not present a
    # previous request's timing, tokens or reset marker as this request's result.
    adapter.last_timing = None
    adapter.last_usage = None
    adapter.last_usage_observation = None
    adapter.last_context_input_tokens = None
    adapter.last_completion_report = None
    adapter.last_reset = False
    # Why this send left the execution unsettled or blocked (None = settled).
    # ChatSession reads it to hold the tracker task instead of completing it.
    adapter.last_execution_hold = None
    if os.environ.get("SOPHIA_G2_DEADLINE_CONTRACT") != "1":
        raise ChatAdapterError("chat recovery requires SOPHIA_G2_DEADLINE_CONTRACT=1")
    if execution_contract is not None and (
            execution_contract.surface != "live_chat" or execution_contract.task_id != task_id):
        raise ValueError("execution contract does not match this chat task")
    timeout = adapter.timeout_seconds
    if execution_contract is not None:
        timeout = min(timeout, execution_contract.require_remaining_s())
    active = _MESSAGE_EXECUTION.get()
    if active is not None:
        if Path(adapter.cwd).resolve() != active.cwd:
            raise ChatAdapterError("message execution belongs to another workspace")
        journal, recovered = active.journal, active.contract.next_attempt(uuid.uuid4().hex)
        recovered.require_remaining_s()
    else:
        journal = ExecutionJournal(Path(adapter.cwd).resolve() / "workspace/runtime/live_chat",
                                   owner_alive=pid_alive)
        try:
            recovered = journal.open("live_chat", CHAT_RECOVERY_KEY, uuid.uuid4().hex, timeout)
        except ReconciliationRequired as exc:
            adapter.last_execution_hold = f"blocked: {exc}"[:300]
            raise ChatExecutionBlocked(f"chat execution blocked: {exc}; inspect the prior execution before reconciling") from exc
    deadline = recovered.deadline_at
    started_at = recovered.started_at
    if execution_contract is not None:
        deadline = min(deadline, execution_contract.deadline_at)
        started_at = min(started_at, execution_contract.started_at)
    budget = ExecutionContract("live_chat", task_id, recovered.attempt_id, started_at, deadline)
    started = time.monotonic()
    journal.attempt_started(CHAT_RECOVERY_KEY, recovered.attempt_id, task_id, task_ref=_TASK_REF.get())
    failure: BaseException | None = None
    before = unknown_fingerprint("not_collected")
    reply, returned = None, False
    try:
        before = _fingerprint(adapter, budget)
        reply = adapter._send(task_id, prompt, context=context, execution_contract=budget)
        returned = True
        return reply
    except BaseException as exc:
        failure = exc
        raise
    finally:
        timing = adapter.last_timing
        if timing is None or timing.start < started:
            outcome, events = None, {}
        else:
            outcome = timing.outcome
            events = {name: at for name, at in timing.events.items() if at >= timing.start}
        settled, detail = settle_attempt(outcome, events, failure)
        # This post-exit observation has a separate <=2s read-only budget. It
        # never extends the provider deadline or changes settled/admission.
        try:
            after = _fingerprint(adapter, budget, after=True)
            axes, evidence = _observation(adapter, before, after, outcome, events, detail, failure, returned, reply)
        except Exception as exc:
            # An observer bug must not skip attempt_finished or mask the send's own failure.
            axes, evidence = None, {"observation_error": type(exc).__name__[:128]}
        journal.attempt_finished(CHAT_RECOVERY_KEY, recovered.attempt_id,
                                 settled=settled, detail=detail, axes=axes, evidence=evidence)
        if active is not None:
            active.unsettled = active.unsettled or not settled
            if not settled:
                adapter.last_execution_hold = f"unsettled: {detail}"[:300]
        else:
            status = journal.close(CHAT_RECOVERY_KEY, "FAILED" if failure else "DONE")
            if status == "NEEDS_RECONCILIATION":
                adapter.last_execution_hold = f"needs reconciliation: {detail}"[:300]
