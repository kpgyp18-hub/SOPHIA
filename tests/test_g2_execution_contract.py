"""G2 C contract: retries inherit the original hard deadline."""
from __future__ import annotations

import pytest

from sophia.runtime.execution_contract import ExecutionContract


def test_retry_keeps_original_deadline_and_refuses_expired_start() -> None:
    first = ExecutionContract.start("live_chat", "task-1", "attempt-1", 10, now=100)
    retry = first.next_attempt("attempt-2")
    assert retry.task_id == first.task_id
    assert retry.deadline_at == first.deadline_at == 110
    assert retry.require_remaining_s(now=107) == 3
    with pytest.raises(ValueError, match="new attempt_id"):
        first.next_attempt("attempt-1")
    with pytest.raises(TimeoutError, match="before new attempt"):
        retry.require_remaining_s(now=110)


@pytest.mark.parametrize("surface,timeout", [("unknown", 5), ("live_chat", 0),
                                              ("live_chat", float("inf"))])
def test_invalid_contract_fails_closed(surface: str, timeout: float) -> None:
    with pytest.raises(ValueError):
        ExecutionContract.start(surface, "task", "attempt", timeout, now=100)


def test_remaining_budget_never_goes_negative() -> None:
    contract = ExecutionContract.start("agent_supervisor", "task", "attempt", 5, now=10)
    assert contract.remaining_s(now=30) == 0
