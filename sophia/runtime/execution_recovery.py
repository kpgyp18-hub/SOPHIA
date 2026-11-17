"""G2 C restart recovery: a durable record of one task budget and its attempts.

A monotonic deadline means nothing in another process, so the record keeps the
wall-clock start and the original budget.  A restarted owner may continue only
with what is left of that budget; it can never be granted more.  An attempt
whose process or side effects are not proven settled blocks every automatic
re-execution of its task until an operator reconciles it.

Nothing here starts, kills or retries a process, and nothing here inspects the
workspace: "settled" means the runner reported a clean exit, not that side
effects were audited.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .execution_boundaries import BOUNDARIES
from .execution_contract import ExecutionContract

RUNNING = "RUNNING"
FINISHED = "FINISHED"
NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

_RECORD_VERSION = 1
_TASK_ID = re.compile(r"[A-Za-z0-9_-]+")
# A restarted owner trusts the wall clock only if it has not gone backwards past
# this tolerance since the record was last written.
_CLOCK_SKEW_TOLERANCE_S = 2.0
_LOCK_WAIT_S = 2.0
_STALE_LOCK_S = 30.0
_REQUIRED = {"version": int, "surface": str, "task_id": str, "generation": int,
             "owner": str, "owner_pid": int, "status": str, "started_wall": float,
             "timeout_s": float, "last_seen_wall": float, "attempts": list,
             "reconciliations": list}


class ReconciliationRequired(RuntimeError):
    """The task must not be executed again until an operator reconciles it."""


class OwnedElsewhere(ReconciliationRequired):
    """Another process holds the record right now; nothing about it may be changed."""


class StaleOwner(RuntimeError):
    """This process no longer owns the record; its update was refused (fencing)."""


class RequestAlreadyFinished(RuntimeError):
    """A persisted request result must be reused rather than executed again."""
    def __init__(self, record: Mapping[str, Any]) -> None:
        super().__init__("request already finished")
        self.record = dict(record)


def settle_attempt(outcome: str | None, events: Mapping[str, Any] | None,
                   failure: BaseException | None) -> tuple[bool, str]:
    """Whether a finished attempt left a provably exited process.

    ``outcome`` is the adapter's timing outcome for *this* attempt (None when
    the adapter failed before it could time anything).  ``events`` must hold
    only this attempt's runner lifecycle events: a runner keeps the previous
    call's events until its next call starts.  Anything not positively proven
    is unsettled.
    """
    events = events or {}
    if "cleanup_incomplete" in events:
        return False, "process cleanup unverified"
    if outcome in ("completed", "nonzero_exit"):
        return True, outcome
    if "process_start" not in events and (
            outcome is None or isinstance(failure, TimeoutError) or "timeout" in events):
        # No timing at all, the budget ran out before the runner was called, or
        # the runner refused an already expired deadline: no child existed.
        return True, "not_started"
    return False, f"{outcome}: interrupted mid-execution; side effects unverified"


class ExecutionJournal:
    """One ``execution.json`` per task under ``root/<task_id>/``."""

    def __init__(self, root: str | Path, *, owner_alive: Callable[[int], bool] | None = None,
                 wall_clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.root = Path(root)
        self.owner = uuid.uuid4().hex
        # Unknown liveness is treated as alive: an interrupted record is then
        # never taken over, only reconciled.
        self._owner_alive = owner_alive or (lambda pid: True)
        self._wall = wall_clock
        self._monotonic = monotonic

    def path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("task_id contains unsupported characters")
        return self.root / task_id / "execution.json"

    def load(self, task_id: str) -> dict[str, Any] | None:
        """The stored record, or None.  An unreadable record needs reconciliation."""
        path = self.path(task_id)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            _validate(record, task_id)
        except (OSError, ValueError) as exc:
            raise ReconciliationRequired(f"execution record is unreadable: {exc}") from exc
        return record

    def open(self, surface: str, task_id: str, attempt_id: str,
             timeout_s: float, *, request_id: str | None = None, payload_hash: str | None = None,
             checkpoint: Mapping[str, Any] | None = None, resume: bool = False) -> ExecutionContract:
        """Start a task budget, or take over an interrupted one that never ran.

        A finished (or absent) record starts a new generation with a new budget:
        that is a new request.  A record left RUNNING by a dead owner is the same
        request, so it keeps its original budget -- and only if no attempt was
        ever started, because completed or interrupted work must not be repeated
        without a decision.
        """
        if surface not in BOUNDARIES:
            raise ValueError("unknown execution surface")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout must be positive and finite")
        if request_id is not None and (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                                       or not re.fullmatch(r"[a-f0-9]{64}", payload_hash or "")):
            raise ValueError("request identity or payload hash is invalid")
        prepared_checkpoint = _copy_checkpoint(dict(checkpoint or {})) if request_id is not None else None
        with self._locked(task_id):
            record = self.load(task_id)
            now_wall = self._wall()
            if request_id is not None:
                previous = self._find_request(task_id, request_id, record)
                if previous is not None:
                    if previous["request"]["payload_hash"] != payload_hash:
                        raise ReconciliationRequired("request id was already used for different content")
                    if previous["status"] == FINISHED:
                        raise RequestAlreadyFinished(previous)
                    if not resume:
                        raise ReconciliationRequired("request is interrupted or active; use explicit resume")
                elif resume:
                    raise ReconciliationRequired("no persisted request to resume")
            if record is None or record["status"] == FINISHED:
                generation = 1
                if record is not None:
                    generation = record["generation"] + 1
                    path = self.path(task_id)
                    os.replace(path, path.with_name(f"execution.g{record['generation']}.json"))
                fresh = {
                    "version": _RECORD_VERSION, "surface": surface, "task_id": task_id,
                    "generation": generation, "owner": self.owner, "owner_pid": os.getpid(),
                    "status": RUNNING, "started_wall": float(now_wall),
                    "timeout_s": float(timeout_s), "last_seen_wall": float(now_wall),
                    "takeovers": 0, "attempts": [], "reconciliations": []}
                if request_id is not None:
                    fresh["request"] = {"id": request_id, "payload_hash": payload_hash,
                                        "checkpoint": prepared_checkpoint, "checkpoint_attempts": 0}
                self._write(task_id, fresh)
                # Wall clock is read first, so a restored deadline is never later
                # than this in-process one.
                return ExecutionContract.start(surface, task_id, attempt_id, timeout_s,
                                               now=self._monotonic())
            if record["surface"] != surface:
                raise ReconciliationRequired("execution record belongs to another surface")
            if request_id is not None and record.get("request", {}).get("id") != request_id:
                raise ReconciliationRequired("another request owns the interrupted execution")
            if record["status"] != RUNNING:
                raise ReconciliationRequired(
                    f"previous execution needs reconciliation: {record.get('reason') or 'unresolved'}")
            if record["owner"] == self.owner or self._owner_alive(record["owner_pid"]):
                raise OwnedElsewhere(
                    f"task is held by a live process (pid {record['owner_pid']})")
            checkpointed = (request_id is not None and resume and
                            record.get("request", {}).get("checkpoint_attempts") == len(record["attempts"]) and
                            all(item.get("settled") is True for item in record["attempts"]))
            if record["attempts"] and not checkpointed:
                return self._block(task_id, record, now_wall,
                                   f"interrupted after {len(record['attempts'])} started attempt(s)")
            if checkpointed and record["request"]["checkpoint"].get("complete") is True:
                record.update(status=FINISHED, terminal=record["request"]["checkpoint"].get("terminal", "DONE"),
                              ended_wall=float(now_wall))
                self._write(task_id, record)
                raise RequestAlreadyFinished(record)
            if now_wall < record["last_seen_wall"] - _CLOCK_SKEW_TOLERANCE_S:
                return self._block(task_id, record, now_wall,
                                   "wall clock moved backwards; remaining budget unknown")
            elapsed = max(now_wall, record["last_seen_wall"]) - record["started_wall"]
            remaining = min(record["timeout_s"] - elapsed, timeout_s)
            if remaining <= 0:
                record.update(status=FINISHED, terminal="EXPIRED", ended_wall=float(now_wall),
                              reason="execution deadline exhausted before restart")
                self._write(task_id, record)
                raise TimeoutError("execution deadline exhausted before restart")
            record.update(owner=self.owner, owner_pid=os.getpid(),
                          last_seen_wall=float(max(now_wall, record["last_seen_wall"])),
                          takeovers=int(record.get("takeovers", 0)) + 1)
            self._write(task_id, record)
            now_mono = self._monotonic()
            return ExecutionContract(surface, task_id, attempt_id,
                                     now_mono - elapsed, now_mono + remaining)

    def mark_interrupted(self, surface: str, task_id: str, reason: str) -> None:
        """Record an interrupted execution that predates this journal, so it stays
        blocked until reconciled instead of being started as if it were new."""
        if surface not in BOUNDARIES:
            raise ValueError("unknown execution surface")
        with self._locked(task_id):
            if self.load(task_id) is not None:
                return
            now_wall = float(self._wall())
            self._write(task_id, {
                "version": _RECORD_VERSION, "surface": surface, "task_id": task_id,
                "generation": 1, "owner": self.owner, "owner_pid": os.getpid(),
                "status": NEEDS_RECONCILIATION, "started_wall": now_wall, "timeout_s": 0.0,
                "last_seen_wall": now_wall, "takeovers": 0, "attempts": [],
                "reconciliations": [], "reason": reason, "ended_wall": now_wall})

    def attempt_started(self, task_id: str, attempt_id: str, label: str) -> None:
        with self._locked(task_id):
            record = self._owned(task_id)
            if any(item.get("attempt_id") == attempt_id for item in record["attempts"]):
                raise ValueError("attempt_id already recorded")
            record["attempts"].append({"attempt_id": attempt_id, "label": label,
                                       "started_wall": float(self._wall()),
                                       "settled": False, "detail": "started"})
            self._touch(task_id, record)

    def attempt_finished(self, task_id: str, attempt_id: str, *, settled: bool,
                         detail: str) -> None:
        with self._locked(task_id):
            record = self._owned(task_id)
            for item in record["attempts"]:
                if item.get("attempt_id") == attempt_id:
                    item.update(settled=bool(settled), detail=detail,
                                ended_wall=float(self._wall()))
                    break
            else:
                raise ValueError("attempt was never recorded as started")
            self._touch(task_id, record)

    def close(self, task_id: str, terminal: str, reason: str | None = None) -> str:
        """Record the owner's terminal state; returns the resulting record status."""
        with self._locked(task_id):
            record = self._owned(task_id)
            unsettled = [item for item in record["attempts"] if not item.get("settled")]
            record["terminal"] = terminal
            record["ended_wall"] = float(self._wall())
            request_gap = ("request" in record and
                           record["request"].get("checkpoint_attempts") != len(record["attempts"]))
            if unsettled or request_gap:
                record["status"] = NEEDS_RECONCILIATION
                record["reason"] = ((f"attempt {unsettled[-1].get('label')}: "
                                     f"{unsettled[-1].get('detail')}") if unsettled
                                    else "last attempt has no durable result checkpoint")
            else:
                record["status"] = FINISHED
                record["reason"] = reason
            self._touch(task_id, record)
            return record["status"]

    def checkpoint(self, task_id: str, snapshot: Mapping[str, Any]) -> None:
        """Acknowledge results and progress only after every started send settled."""
        value = _copy_checkpoint(snapshot)
        with self._locked(task_id):
            record = self._owned(task_id)
            if "request" not in record:
                raise ValueError("execution has no request identity")
            if any(item.get("settled") is not True for item in record["attempts"]):
                raise ReconciliationRequired("unsettled execution cannot be checkpointed")
            record["request"].update(checkpoint=value, checkpoint_attempts=len(record["attempts"]))
            self._touch(task_id, record)

    def request_record(self, task_id: str, request_id: str) -> dict[str, Any] | None:
        """Read current or archived request metadata without creating a missing root."""
        if not self.path(task_id).parent.is_dir():
            return None
        with self._locked(task_id):
            return self._find_request(task_id, request_id, self.load(task_id))

    def _find_request(self, task_id: str, request_id: str, current) -> dict[str, Any] | None:
        directory = self.path(task_id).parent
        if any(directory.glob("execution.unreadable-*.json")):
            raise ReconciliationRequired("request history is unreadable; uniqueness cannot be verified")
        if current is not None and current.get("request", {}).get("id") == request_id:
            return current
        for path in directory.glob("execution.g*.json"):
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                _validate(previous, task_id)
            except (OSError, ValueError) as exc:
                raise ReconciliationRequired("archived request history is unreadable") from exc
            if previous.get("request", {}).get("id") == request_id:
                return previous
        return None

    def reconcile(self, task_id: str, note: str) -> dict[str, Any]:
        """An operator states the previous execution was inspected and is settled."""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("a reconciliation note is required")
        path = self.path(task_id)
        if not path.is_file():      # checked before locking: an unknown task creates nothing
            raise ValueError("no execution record to reconcile")
        with self._locked(task_id, break_stale=True):
            if not path.is_file():
                raise ValueError("no execution record to reconcile")
            now_wall = float(self._wall())
            entry = {"at_wall": now_wall, "by_pid": os.getpid(), "note": note.strip()}
            try:
                record = self.load(task_id)
            except ReconciliationRequired as exc:
                # Keep the unreadable file as evidence; the task is released.
                os.replace(path, path.with_name(f"execution.unreadable-{int(now_wall)}.json"))
                record = {"version": _RECORD_VERSION, "surface": "", "task_id": task_id,
                          "generation": 0, "owner": self.owner, "owner_pid": os.getpid(),
                          "status": NEEDS_RECONCILIATION, "started_wall": now_wall,
                          "timeout_s": 0.0, "last_seen_wall": now_wall, "takeovers": 0,
                          "attempts": [], "reconciliations": [], "reason": str(exc)}
            if record["status"] == FINISHED:
                raise ValueError("execution record is already settled")
            if (record["status"] == RUNNING and record["owner"] != self.owner
                    and self._owner_alive(record["owner_pid"])):
                raise ReconciliationRequired(
                    f"owner process {record['owner_pid']} is still alive; stop it first")
            entry.update(from_status=record["status"], from_reason=record.get("reason"))
            record["reconciliations"].append(entry)
            record.update(status=FINISHED, terminal="RECONCILED", ended_wall=now_wall,
                          owner=self.owner, owner_pid=os.getpid(),
                          last_seen_wall=float(max(now_wall, record["last_seen_wall"])))
            self._write(task_id, record)
            return record

    # -- internals ---------------------------------------------------------

    def _block(self, task_id: str, record: dict[str, Any], now_wall: float, reason: str):
        record.update(status=NEEDS_RECONCILIATION, reason=reason, ended_wall=float(now_wall))
        self._write(task_id, record)
        raise ReconciliationRequired(f"previous execution needs reconciliation: {reason}")

    def _owned(self, task_id: str) -> dict[str, Any]:
        record = self.load(task_id)
        if record is None or record["owner"] != self.owner or record["status"] != RUNNING:
            raise StaleOwner("execution record is not owned by this process")
        return record

    def _touch(self, task_id: str, record: dict[str, Any]) -> None:
        record["last_seen_wall"] = float(max(self._wall(), record["last_seen_wall"]))
        self._write(task_id, record)

    def _write(self, task_id: str, record: Mapping[str, Any]) -> None:
        path = self.path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".execution.", suffix=".tmp",
                                                 dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def _locked(self, task_id: str, *, break_stale: bool = False) -> Iterator[None]:
        """Serialize read-modify-write across processes with an exclusive lock file."""
        lock = self.path(task_id).with_name("execution.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        give_up = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                if break_stale:
                    try:
                        if time.time() - lock.stat().st_mtime > _STALE_LOCK_S:
                            lock.unlink()
                            continue
                    except FileNotFoundError:
                        continue
                if time.monotonic() >= give_up:
                    raise OwnedElsewhere("execution record is locked by another process")
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def _validate(record: Any, task_id: str) -> None:
    if not isinstance(record, dict):
        raise ValueError("record is not an object")
    for key, kind in _REQUIRED.items():
        value = record.get(key)
        if kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} is not a finite number")
        elif isinstance(value, bool) or not isinstance(value, kind):
            raise ValueError(f"{key} has the wrong type")
    if record["version"] != _RECORD_VERSION:
        raise ValueError("unsupported record version")
    if record["task_id"] != task_id:
        raise ValueError("record belongs to another task")
    if record["status"] not in (RUNNING, FINISHED, NEEDS_RECONCILIATION):
        raise ValueError("unknown record status")
    if not all(isinstance(item, dict) for item in record["attempts"]):
        raise ValueError("attempts are malformed")
    if "request" in record:
        request = record["request"]
        if (not isinstance(request, dict) or not isinstance(request.get("id"), str)
                or not isinstance(request.get("payload_hash"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request["id"])
                or not re.fullmatch(r"[a-f0-9]{64}", request["payload_hash"])
                or type(request.get("checkpoint_attempts")) is not int
                or not 0 <= request["checkpoint_attempts"] <= len(record["attempts"])):
            raise ValueError("request metadata is malformed")
        _copy_checkpoint(request.get("checkpoint"))


def _copy_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("checkpoint must be an object")
    data = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(data.encode("utf-8")) > 262144:
        raise ValueError("checkpoint exceeds 256 KiB")
    return json.loads(data)
