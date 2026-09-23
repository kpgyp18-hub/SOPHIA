import pytest

from sophia.safety.classifier import SafetyAssessment, SafetyVerdict
from sophia.safety.manager import SafetyManager


class StubClassifier:
    def __init__(self, assessment):
        self.assessment, self.calls = assessment, []

    def classify(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return self.assessment


def test_r0_safe_fast_result_does_not_cold_load_deep_model():
    fast = StubClassifier(SafetyAssessment(SafetyVerdict.SAFE))
    deep = StubClassifier(SafetyAssessment(SafetyVerdict.SAFE))
    decision = SafetyManager(fast, deep).assess("harmless", risk="R0")
    assert decision.allowed and decision.deep is None
    assert len(fast.calls) == 1 and not deep.calls


def test_unknown_fast_result_escalates_and_remains_fail_closed_when_deep_fails():
    fast = StubClassifier(SafetyAssessment(SafetyVerdict.UNKNOWN))
    deep = StubClassifier(SafetyAssessment(SafetyVerdict.UNKNOWN))
    decision = SafetyManager(fast, deep).assess("ambiguous", risk="R1")
    assert decision.deep is not None and not decision.allowed
    assert decision.escalation_reason == "fast safety returned unknown"


def test_r2_always_uses_deep_safety_even_when_fast_is_safe():
    fast = StubClassifier(SafetyAssessment(SafetyVerdict.SAFE))
    deep = StubClassifier(SafetyAssessment(SafetyVerdict.UNSAFE))
    decision = SafetyManager(fast, deep).assess("write request", risk="R2")
    assert decision.assessment.verdict is SafetyVerdict.UNSAFE
    assert decision.escalation_reason == "R2 policy requires deep safety"
    assert len(deep.calls) == 1


def test_unknown_risk_is_rejected_before_any_model_call():
    fast = StubClassifier(SafetyAssessment(SafetyVerdict.SAFE))
    deep = StubClassifier(SafetyAssessment(SafetyVerdict.SAFE))
    with pytest.raises(ValueError, match="unsupported risk level"):
        SafetyManager(fast, deep).assess("x", risk="R9")
    assert not fast.calls and not deep.calls
