from __future__ import annotations

from sophia.decisions.base import DecisionOutcome


class RuleDecisionProvider:
    """Deterministic local fallback; it neither calls a model nor external service."""

    provider = "rules"

    def decide(self, user_input: str) -> DecisionOutcome:
        text = user_input.lower()
        coding = (
            "bug", "error", "debug", "code", "file", "function", "python", "write", "create",
            "modify", "edit", "add", "test", ".py",
            "코드", "파일", "함수", "수정", "편집", "추가", "작성", "구현", "테스트", "디버그",
            "버그", "오류", "에러", "고쳐", "고치",
        )
        debug = ("bug", "error", "debug", "버그", "오류", "에러", "디버그")
        architecture = ("architecture", "plan", "review", "design", "risk", "구조", "설계", "계획", "검토")
        if any(term in text for term in coding):
            task_type = "debug" if any(term in text for term in debug) else "coding"
            complexity = 0.30
        elif any(term in text for term in architecture):
            task_type, complexity = "architecture", 0.60
        else:
            task_type, complexity = "chat", 0.30
        return DecisionOutcome(task_type, complexity, 1.0, self.provider, "deterministic local rule match")
