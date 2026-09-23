"""In-memory sequential writer gate; it deliberately has no concurrent mode."""
from dataclasses import dataclass, field
from typing import Any
class WriterBusyError(RuntimeError): pass
@dataclass
class WriterLock:
    _active: dict[str, Any] = field(default_factory=dict)
    def acquire(self, task_id: str, writer: Any) -> None:
        current = self._active.get(task_id)
        if current is not None and _is_running(current): raise WriterBusyError(f"task {task_id!r} already has an active writer")
        self._active[task_id] = writer
    def release_completed(self, task_id: str) -> bool:
        current = self._active.get(task_id)
        if current is None: return True
        if _is_running(current): return False
        del self._active[task_id]; return True
    def can_start_next(self, task_id: str) -> bool: return self.release_completed(task_id)
    def active(self, task_id: str) -> Any | None:
        return self._active.get(task_id)
def _is_running(process: Any) -> bool:
    poll = getattr(process, "poll", None)
    return poll() is None if poll is not None else bool(getattr(process, "is_running", False))
