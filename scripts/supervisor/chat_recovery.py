"""Opt-in durable admission for live chat sends; Runner still owns processes.

One journal key per cwd prevents a new provider or a changed task id from
bypassing unresolved work after a host restart. A record describes a logical
send, including its existing bounded resume retries, not each subprocess.
"""
from __future__ import annotations

import os
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

if TYPE_CHECKING:
    from .chat_adapter import ChatAdapter

CHAT_RECOVERY_KEY = "live-chat"
_MESSAGE_EXECUTION: ContextVar = ContextVar("sophia_message_execution", default=None)


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


def recoverable_send(adapter: ChatAdapter, task_id: str, prompt: str, *, context: str | None = None,
                     execution_contract: ExecutionContract | None = None) -> str:
    from .chat_adapter import ChatAdapterError

    # Admission can fail before _send resets per-call state. Do not present a
    # previous request's timing, tokens or reset marker as this request's result.
    adapter.last_timing = None
    adapter.last_usage = None
    adapter.last_usage_observation = None
    adapter.last_context_input_tokens = None
    adapter.last_completion_report = None
    adapter.last_reset = False
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
            raise ChatAdapterError(f"chat execution blocked: {exc}; inspect the prior execution before reconciling") from exc
    deadline = recovered.deadline_at
    started_at = recovered.started_at
    if execution_contract is not None:
        deadline = min(deadline, execution_contract.deadline_at)
        started_at = min(started_at, execution_contract.started_at)
    budget = ExecutionContract("live_chat", task_id, recovered.attempt_id, started_at, deadline)
    started = time.monotonic()
    journal.attempt_started(CHAT_RECOVERY_KEY, recovered.attempt_id, task_id)
    failure: BaseException | None = None
    try:
        return adapter._send(task_id, prompt, context=context, execution_contract=budget)
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
        journal.attempt_finished(CHAT_RECOVERY_KEY, recovered.attempt_id,
                                 settled=settled, detail=detail)
        if active is not None:
            active.unsettled = active.unsettled or not settled
        else:
            journal.close(CHAT_RECOVERY_KEY, "FAILED" if failure else "DONE")
