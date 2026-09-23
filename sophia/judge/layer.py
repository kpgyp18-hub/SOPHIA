"""Host-owned Judge Layer for claims made after a tool action.

Safety decides whether an action may happen; this layer decides whether its
claimed outcome is enough to commit a checkpoint. The default rule judge is
deterministic and offline; a model-backed implementation can be injected
without granting the model execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class JudgeVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class JudgeDecision:
    approved: bool
    criterion: str
    reason: str
    verdict: JudgeVerdict = JudgeVerdict.PASS


class JudgeLayer(Protocol):
    """Validate bounded tool/result evidence before a commit is accepted."""

    def validate_tool_call(self, *, tool: str, arguments: object) -> JudgeDecision: ...
    def validate_worker_result(self, *, result: object) -> JudgeDecision: ...
    def validate_test_claim(self, *, passed: bool | None, summary: str, changed_path: str) -> JudgeDecision: ...
    def validate_groundedness(self, *, answer: str, sources: tuple[str, ...]) -> JudgeDecision: ...


class RuleJudge:
    """Fail-closed local baseline for evidence that needs no model inference."""

    def validate_tool_call(self, *, tool: str, arguments: object) -> JudgeDecision:
        valid = bool(tool.strip()) and arguments is not None
        return JudgeDecision(valid, "tool_call", "registered tool and bounded arguments" if valid else "missing tool evidence")

    def validate_worker_result(self, *, result: object) -> JudgeDecision:
        valid = result is not None
        return JudgeDecision(valid, "worker_result", "worker returned a result" if valid else "worker result is absent")

    def validate_test_claim(self, *, passed: bool | None, summary: str, changed_path: str) -> JudgeDecision:
        evidence = bool(summary.strip()) and bool(changed_path.strip())
        if passed is True and evidence:
            return JudgeDecision(True, "test_claim", "test result supports the changed-file claim", JudgeVerdict.PASS)
        # A project without a test suite cannot fabricate a passing claim.  The
        # bounded runner marks this distinct, and the handoff retains the
        # skipped-test evidence for later human review.
        if passed is None and evidence and summary.startswith("No tests/ directory configured"):
            return JudgeDecision(False, "test_claim", "no project test suite is configured; user decision is required", JudgeVerdict.UNCERTAIN)
        return JudgeDecision(False, "test_claim", "test result does not support commit", JudgeVerdict.FAIL)

    def validate_groundedness(self, *, answer: str, sources: tuple[str, ...]) -> JudgeDecision:
        valid = bool(answer.strip()) and bool(sources)
        return JudgeDecision(valid, "rag_groundedness", "answer has supplied sources" if valid else "answer lacks source evidence")
