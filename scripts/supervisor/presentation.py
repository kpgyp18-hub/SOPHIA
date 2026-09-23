"""Human-readable terminal status signals for Supervisor execution."""
from __future__ import annotations

from typing import Callable

_FORWARD_ARROW = "->"
_RECEIVED_ARROW = "<-"
_before_emit_hook: Callable[[], None] | None = None

def set_before_emit_hook(hook: Callable[[], None] | None) -> None:
    """Registers a callback invoked right before every presentation line is printed.
    Used by --watch mode to flush a still-open streamed-text line (no trailing
    newline yet) before a status marker interrupts it, so output doesn't run
    together on one line. Pass None to clear it."""
    global _before_emit_hook
    _before_emit_hook = hook

def emit(event: str, **fields: object) -> None:
    if _before_emit_hook is not None:
        _before_emit_hook()
    detail = " ".join(str(value) for value in fields.values() if value is not None)
    print(f"[{event}]" + (f" {detail}" if detail else ""))

def _step_fields(step: int | None, total: int | None) -> dict[str, object]:
    """Return optional route progress without changing legacy output."""
    if step is not None and total is not None:
        return {"step": f"Step {step}/{total}"}
    return {}


def emit_start(agent: str, role: str, step: int | None = None, total: int | None = None) -> None:
    emit("START", **_step_fields(step, total), agent=agent, arrow=_FORWARD_ARROW, role=role)


def emit_running(agent: str, role: str, step: int | None = None, total: int | None = None) -> None:
    emit("RUNNING", **_step_fields(step, total), agent=agent, arrow=_FORWARD_ARROW, role=role)
def emit_handoff(source_agent: str, target_agent: str, reason_code: str) -> None: emit(f"HANDOFF {_FORWARD_ARROW} {target_agent}", reason=reason_code)
def emit_handoff_received(source_agent: str, target_agent: str, role: str) -> None: emit(f"HANDOFF RECEIVED {_RECEIVED_ARROW} {source_agent}", agent=target_agent, arrow=_FORWARD_ARROW, role=role)
def emit_test(agent: str) -> None: emit("TEST", agent=agent, arrow=_FORWARD_ARROW, role="validation")
def emit_review(agent: str) -> None: emit("REVIEW", agent=agent, arrow=_FORWARD_ARROW, role="review")
def emit_retry(agent: str, current: int, maximum: int) -> None: emit(f"RETRY {current}/{maximum}", agent=agent)
def emit_need_user(reason: str) -> None: emit("NEED_USER", reason=reason)
def emit_failed(reason: str) -> None: emit("FAILED", reason=reason)
def emit_done() -> None: emit("DONE", message="Task completed")
def emit_model(agent: str, tier: str) -> None: emit("MODEL", agent=agent, arrow=_FORWARD_ARROW, tier=tier)
def emit_timing(summary: str) -> None: emit("TIMING", detail=summary)

def emit_message(agent: str, output: dict) -> None:
    """Print what the agent actually decided/said for this step, not just the status marker."""
    if _before_emit_hook is not None:
        _before_emit_hook()
    handoff = output.get("handoff_request")
    if handoff:
        scope = handoff.get("scope") or "(scope not given)"
        print(f'[SAYS] {agent} -> {handoff["target_agent"]} ({handoff["reason_code"]}):\n    "{scope}"')
    else:
        print(f'[SAYS] {agent}: status={output.get("status")} (no further handoff)')
