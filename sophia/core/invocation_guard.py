"""Bounded, stack-scoped protection against recursive provider invocation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import uuid4


class RecursiveInvocationError(RuntimeError):
    pass


class InvocationDepthError(RecursiveInvocationError):
    pass


@dataclass(frozen=True)
class InvocationFrame:
    call_id: str
    root_task_id: str | None
    parent_call_id: str | None
    provider: str
    worker: str | None
    depth: int
    external_hops: int


@dataclass
class InvocationGuard:
    """Tracks active calls only; sequential reuse of one provider remains valid."""

    max_depth: int = 2
    _frames: list[InvocationFrame] = field(default_factory=list, init=False)

    def enter(self, provider: str, *, worker: str | None = None,
              root_task_id: str | None = None, parent_call_id: str | None = None,
              external: bool = False) -> str:
        if len(self._frames) >= self.max_depth:
            raise InvocationDepthError(f"invocation depth exceeds {self.max_depth}")
        if provider in self.visited_providers:
            raise RecursiveInvocationError(f"recursive provider invocation rejected: {provider}")
        if worker and worker in self.visited_workers:
            raise RecursiveInvocationError(f"recursive worker invocation rejected: {worker}")
        call_id = f"call-{uuid4().hex[:12]}"
        parent = parent_call_id or (self._frames[-1].call_id if self._frames else None)
        hops = sum(1 for frame in self._frames if frame.provider == "external") + int(external)
        self._frames.append(InvocationFrame(call_id, root_task_id, parent, provider, worker,
                                            len(self._frames), hops))
        return call_id

    def leave(self, provider: str, *, worker: str | None = None) -> None:
        if not self._frames or self._frames[-1].provider != provider:
            raise RecursiveInvocationError(f"invocation stack mismatch for provider: {provider}")
        if worker and self._frames[-1].worker != worker:
            raise RecursiveInvocationError(f"invocation stack mismatch for worker: {worker}")
        self._frames.pop()

    @property
    def visited_providers(self) -> frozenset[str]:
        return frozenset(frame.provider for frame in self._frames)

    @property
    def visited_workers(self) -> frozenset[str]:
        return frozenset(frame.worker for frame in self._frames if frame.worker)

    def snapshot(self) -> list[dict[str, object]]:
        return [asdict(frame) for frame in self._frames]
