from __future__ import annotations

from sophia.core.escalation import EscalationDecision, EscalationPolicy
from sophia.decisions.base import DecisionOutcome
from sophia.runtime.task import Task, TaskStatus, TaskType


class RoutingDecision:
    def __init__(self, task: Task, supervisor: str, brain: str, context_class: str,
                 escalation: EscalationDecision) -> None:
        self.task, self.supervisor, self.brain, self.context_class = task, supervisor, brain, context_class
        self.escalation = escalation


class Router:
    def classify(self, text: str, hint: DecisionOutcome | None = None) -> Task:
        if hint is not None:
            kind = TaskType(hint.task_type)
            task = Task(text, kind, hint.complexity)
            task.status = TaskStatus.CLASSIFIED
            return task
        lowered = text.lower()
        coding_terms = (
            "bug", "error", "debug", "code", "file", "function", "python", "write", "create",
            "modify", "edit", "add", "test", ".py",
            "코드", "파일", "함수", "수정", "편집", "추가", "작성", "구현", "테스트", "디버그",
            "버그", "오류", "에러", "고쳐", "고치",
        )
        debug_terms = ("bug", "error", "debug", "버그", "오류", "에러", "디버그")
        architecture_terms = ("architecture", "plan", "review", "구조", "설계", "계획", "검토")
        if any(word in lowered for word in coding_terms):
            kind = TaskType.DEBUG if any(word in lowered for word in debug_terms) else TaskType.CODING
        elif any(word in lowered for word in architecture_terms):
            kind = TaskType.ARCHITECTURE
        else:
            kind = TaskType.CHAT
        task = Task(text, kind, 0.6 if kind is TaskType.ARCHITECTURE else 0.3)
        task.status = TaskStatus.CLASSIFIED
        return task

    @staticmethod
    def supervisor(task: Task) -> str:
        return "blanc" if task.task_type in (TaskType.ARCHITECTURE, TaskType.REVIEW) else "aide"

    @staticmethod
    def brain(task: Task) -> str:
        return "coder" if task.task_type in (TaskType.CODING, TaskType.DEBUG) else "general"

    def decide(self, text: str, *, coding_continuation: bool = False,
               hint: DecisionOutcome | None = None) -> RoutingDecision:
        task = self.classify(text, hint)
        if coding_continuation and task.task_type is TaskType.CHAT:
            task.task_type = TaskType.CODING
            task.complexity = 0.3
        context_class = "complex" if task.complexity >= 0.5 else "normal"
        return RoutingDecision(task, self.supervisor(task), self.brain(task), context_class,
                               EscalationPolicy().plan(task))
