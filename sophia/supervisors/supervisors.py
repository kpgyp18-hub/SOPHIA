from __future__ import annotations

from sophia.brains.ollama import Brain, BrainResult
from sophia.runtime.task import Task


class Supervisor:
    name = "supervisor"
    instruction = "Give a concise, practical answer."

    def run(self, task: Task, brain: Brain, context: str) -> BrainResult:
        prompt = f"You are {self.name}. {self.instruction}\nContext:\n{context}\nUser task:\n{task.input}\nAnswer:"
        return brain.generate(prompt)


class Blanc(Supervisor):
    name = "Blanc"
    instruction = "Plan, assess risk, and explain the next decision clearly."


class Aide(Supervisor):
    name = "Aide"
    instruction = ("Execute the request precisely. For an existing requested file, output one fenced `diff` "
                   "unified diff with matching context. For a new file, output its complete content in one fenced "
                   "code block. Never claim a file was changed yourself.")
