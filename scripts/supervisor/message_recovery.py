"""Opt-in message checkpoints in the existing execution journal.

The journal remains the execution source of truth. ChatTaskTracker remains an
observer. No subprocess owner or automatic retry loop is introduced.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from sophia.runtime.execution_recovery import (ExecutionJournal, ReconciliationRequired,
                                               RequestAlreadyFinished)
from .chat_adapter import ChatAdapter
from .chat_recovery import CHAT_RECOVERY_KEY, message_execution
from .long_task import pid_alive

FLAG = "SOPHIA_G2_MESSAGE_RECOVERY"


def enabled() -> bool:
    return os.environ.get(FLAG) == "1"


def _payload(session, target: str, message: str) -> dict:
    return {"target": target, "message": message, "relay": session.auto_relay_enabled,
            "models": {name: getattr(agent, "model", None) for name, agent in session.agents.items()}}


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _snapshot(session, state: dict) -> dict:
    return {**state, "history": [{"speaker": turn.speaker, "text": turn.text,
                                  "at": turn.at.isoformat()} for turn in session.history],
            "summary": session.history_summary, "tracker_task_id": session._active_task_id}


def _validate(state: dict, session) -> None:
    try:
        _validate_checkpoint(state, session)
    except (TypeError, ValueError, KeyError) as exc:
        raise ReconciliationRequired("message checkpoint is malformed") from exc


def _validate_checkpoint(state: dict, session) -> None:
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("payload"), dict)
            or not isinstance(state.get("history"), list)
            or not isinstance(state.get("summary"), str)
            or not isinstance(state.get("pending"), list)
            or not isinstance(state.get("replies"), list)
            or type(state.get("hops")) is not int or not 0 <= state["hops"] <= 4
            or type(state.get("started")) is not bool or type(state.get("complete")) is not bool
            or type(state.get("had_error")) is not bool):
        raise ReconciliationRequired("message checkpoint is malformed")
    payload = state["payload"]
    if (not isinstance(payload.get("message"), str) or payload.get("target") not in (*session.agents, "Both")
            or type(payload.get("relay")) is not bool or not isinstance(payload.get("models"), dict)
            or len(state["pending"]) > 2 or len(state["history"]) > 41 or len(state["replies"]) > 6):
        raise ReconciliationRequired("message checkpoint has invalid routing or history")
    if any(name not in session.agents for name in state["pending"]):
        raise ReconciliationRequired("message checkpoint references an unavailable agent")
    for turn in [*state["history"], *state["replies"]]:
        if not isinstance(turn, dict) or not isinstance(turn.get("speaker"), str) or not isinstance(turn.get("text"), str):
            raise ReconciliationRequired("message history is malformed")
        if "at" in turn:
            datetime.fromisoformat(turn["at"])
    if any(not isinstance(turn.get("at"), str) for turn in state["history"]):
        raise ReconciliationRequired("message history timestamps are missing")
    if state["complete"] != (not state["pending"]) or (state["complete"] and state.get("terminal") not in ("DONE", "FAILED")):
        raise ReconciliationRequired("message completion checkpoint is inconsistent")


def _restore(session, state: dict) -> None:
    from .chat_session import Turn
    session.history = [Turn(turn["speaker"], turn["text"], datetime.fromisoformat(turn["at"]))
                       for turn in state["history"]]
    session.history_summary = state["summary"]
    session._active_task_id = state.get("tracker_task_id")
    session._last_sent_index = {name: 0 for name in session.agents}
    for agent in session.agents.values():
        agent.telemetry_reset_reason = "message_resume"
        agent.reset_session()


def run_message(session, target: str | None, message: str = "", *, request_id: str | None = None,
                resume: bool = False) -> bool:
    from .chat_session import _addressed_agent, _MAX_AUTO_RELAY_HOPS
    journal = None
    owns_record = False
    accepted = False
    pending_attachments = None
    terminal = "FAILED"
    # This request owns its tracker hold; a previous request's hold must not leak in.
    session._execution_hold = None
    try:
        if any(os.environ.get(flag) != "1" for flag in (
                "SOPHIA_G2_CHAT_RECOVERY", "SOPHIA_G2_DEADLINE_CONTRACT")):
            raise ValueError("message recovery requires chat recovery and the shared deadline flags")
        root = session.attachment_root.resolve()
        if any(not isinstance(agent, ChatAdapter) or Path(agent.cwd).resolve() != root
               for agent in session.agents.values()):
            raise ValueError("message recovery requires chat adapters sharing the chat workspace")
        journal = ExecutionJournal(root / "workspace/runtime/live_chat", owner_alive=pid_alive)
        request_id = request_id or uuid.uuid4().hex
        session.printer("System", f"request {request_id}")
        if resume:
            previous = journal.request_record(CHAT_RECOVERY_KEY, request_id)
            if previous is None:
                raise ValueError("request was not found")
            state = previous["request"]["checkpoint"]
            _validate(state, session)
            payload = state["payload"]
            if payload != _payload(session, payload["target"], payload["message"]):
                raise ValueError("request policy changed; preserve the prior request and use a new id")
        else:
            if target not in (*session.agents, "Both"):
                raise ValueError("unknown request target")
            pending_attachments = list(session._pending_attachments)
            message = session._take_attachments(message)
            payload = _payload(session, target, message)
            state = _snapshot(session, {"version": 1, "payload": payload,
                                      "pending": list(session.agents) if target == "Both" else [target],
                                      "replies": [], "hops": 0, "started": False,
                                      "had_error": False, "complete": False})
        budget = journal.open("live_chat", CHAT_RECOVERY_KEY, uuid.uuid4().hex,
                              min(agent.timeout_seconds for agent in session.agents.values()),
                              request_id=request_id, payload_hash=_hash(payload), checkpoint=state, resume=resume)
        owns_record = True
        accepted = True
        if resume:
            state = journal.load(CHAT_RECOVERY_KEY)["request"]["checkpoint"]
            _validate(state, session)
            _restore(session, state)
        if not state["started"]:
            session._record("You", payload["message"])
            session._compact_history()
            # The request body's @mention is not kept in the payload, so approval basis is unknown.
            session._begin_task(payload["message"], target=payload["target"], explicit=None)
            state["started"] = True
            journal.checkpoint(CHAT_RECOVERY_KEY, _snapshot(session, state))
        with message_execution(journal, budget, root) as execution:
            while state["pending"]:
                if budget.remaining_s() <= 0:
                    terminal = "EXPIRED"
                    session.printer("System", "request deadline exhausted; no next agent started")
                    break
                name = state["pending"][0]
                agent, prompt = session._prepare_request(name)
                context = f"relay {state['hops']}/{_MAX_AUTO_RELAY_HOPS}" if state["hops"] else None
                reply = session._request(agent, name, prompt, context=context,
                                         deadline=(budget.started_at, budget.deadline_at))
                session._complete_request(name, reply)
                session._print_timing(agent)
                session._print_usage(name, agent)
                if execution.unsettled:
                    terminal = "NEED_USER"
                    session.printer("System", "request requires reconciliation; no automatic resume")
                    break
                state["replies"].append({"speaker": name, "text": reply})
                state["had_error"] = state["had_error"] or getattr(agent, "last_send_failed", False)
                state["pending"].pop(0)
                relay = _addressed_agent(reply) if payload["relay"] and payload["target"] != "Both" else None
                if relay is not None and relay != name:
                    if state["hops"] < _MAX_AUTO_RELAY_HOPS:
                        state["hops"] += 1
                        state["pending"].append(relay)
                        observe = getattr(session.task_tracker, "handoff", None)
                        if callable(observe):
                            observe(session._active_task_id, name, relay, state["hops"])
                    else:
                        session.printer("System", "automatic relay stopped: hop limit reached")
                state["complete"] = not state["pending"]
                state["terminal"] = "FAILED" if state["had_error"] else "DONE"
                journal.checkpoint(CHAT_RECOVERY_KEY, _snapshot(session, state))
            else:
                terminal = state["terminal"]
                if terminal == "DONE":
                    session._complete_task()
        return True
    except RequestAlreadyFinished as cached:
        accepted = True
        state = cached.record["request"]["checkpoint"]
        try:
            _validate(state, session)
            if _hash(state["payload"]) != cached.record["request"]["payload_hash"]:
                raise ReconciliationRequired("saved request payload no longer matches its identity")
            if not session.history:
                _restore(session, state)
                session._active_task_id = None
        except (ReconciliationRequired, ValueError, KeyError) as exc:
            session.printer("System", f"saved request result is unreadable: {exc}")
            return True
        session.printer("System", f"request {request_id}: {cached.record.get('terminal')}; reusing saved result")
        for reply in state["replies"]:
            session.printer(reply["speaker"], reply["text"])
        if cached.record.get("terminal") == "DONE":
            complete = getattr(session.task_tracker, "complete", None)
            if callable(complete):
                complete(state.get("tracker_task_id"))
        return True
    except (ReconciliationRequired, ValueError, OSError) as exc:
        session.printer("System", f"message request refused: {exc}")
        return True
    finally:
        if not accepted and pending_attachments is not None:
            session._pending_attachments = pending_attachments
        if owns_record:
            status = journal.close(CHAT_RECOVERY_KEY, terminal)
            if status == "NEEDS_RECONCILIATION" or terminal != "DONE":
                hold = getattr(session.task_tracker, "hold", None)
                if callable(hold):
                    hold(session._active_task_id, f"request {request_id}: {status}/{terminal}")
            session._active_task_id = None
