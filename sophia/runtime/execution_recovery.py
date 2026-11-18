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
import hashlib
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

# Terminal labels an owner may close with; RECONCILED and ACKNOWLEDGED_HISTORY
# are written only by the operator paths below. See output/STATE_MAPPING_DESIGN_20261118.md.
OWNER_TERMINALS = frozenset({"DONE", "FAILED", "NEED_USER", "EXPIRED"})
TERMINALS = OWNER_TERMINALS | {"RECONCILED", "ACKNOWLEDGED_HISTORY"}

_RECORD_VERSION = 1
_TASK_ID = re.compile(r"[A-Za-z0-9_-]+")
# A restarted owner trusts the wall clock only if it has not gone backwards past
# this tolerance since the record was last written.
_CLOCK_SKEW_TOLERANCE_S = 2.0
_LOCK_WAIT_S = 2.0
_STALE_LOCK_S = 30.0
_EVIDENCE_LIMIT = 16384
_INDEX_TERMINAL_CHARS = 64
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
        self._index_state = {}

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
            self._index_state.pop(task_id, None)
            record = self.load(task_id)
            now_wall = self._wall()
            if request_id is None and record is not None and "request" in record:
                raise ReconciliationRequired("request-bound execution requires the request recovery path")
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
                if request_id is not None:
                    rows, index_meta = self._index_state.get(task_id) or self._archive_rows(task_id, record)
                else:
                    rows, index_meta = [], None
                generation = self._next_generation(task_id, record)
                if record is not None:
                    path = self.path(task_id)
                    archive = path.with_name(f"execution.g{record['generation']}.json")
                    if archive.exists():
                        raise ReconciliationRequired("generation archive already exists; preserve it before proceeding")
                    os.replace(path, archive)
                    if request_id is not None:
                        index_meta = self._store_index(task_id, rows + [self._index_entry(record, archive)], append=index_meta)
                fresh = {
                    "version": _RECORD_VERSION, "surface": surface, "task_id": task_id,
                    "generation": generation, "owner": self.owner, "owner_pid": os.getpid(),
                    "status": RUNNING, "started_wall": float(now_wall),
                    "timeout_s": float(timeout_s), "last_seen_wall": float(now_wall),
                    "takeovers": 0, "attempts": [], "reconciliations": []}
                if index_meta is not None:
                    fresh["archive_index"] = index_meta
                if record is not None and record.get("history_acknowledgements"):
                    fresh["history_acknowledgements"] = record["history_acknowledgements"]
                    fresh["request_history_incomplete"] = True
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
                saved = record["request"]["checkpoint"].get("terminal", "DONE")
                record.update(status=FINISHED, terminal=saved if saved in OWNER_TERMINALS else "FAILED",
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
                         detail: str, axes: Mapping[str, Any] | None = None,
                         evidence: Mapping[str, Any] | None = None) -> None:
        observed_axes = _copy_axes(axes) if axes is not None else None
        observed_evidence = _bounded_evidence(evidence) if evidence is not None else None
        with self._locked(task_id):
            record = self._owned(task_id)
            for item in record["attempts"]:
                if item.get("attempt_id") == attempt_id:
                    item.update(settled=bool(settled), detail=detail,
                                ended_wall=float(self._wall()))
                    if observed_axes is not None:
                        item["axes"] = observed_axes
                    if observed_evidence is not None:
                        item["evidence"] = observed_evidence
                    break
            else:
                raise ValueError("attempt was never recorded as started")
            self._touch(task_id, record)

    def close(self, task_id: str, terminal: str, reason: str | None = None) -> str:
        """Record the owner's terminal state; returns the resulting record status."""
        with self._locked(task_id):
            record = self._owned(task_id)
            unsettled = [item for item in record["attempts"] if not item.get("settled")]
            # close() runs in finally blocks, so an unknown label is normalized
            # conservatively instead of raising and leaving the record RUNNING.
            if terminal in OWNER_TERMINALS:
                record["terminal"] = terminal
            else:
                record["terminal"] = "FAILED"
                record["terminal_original"] = (terminal if isinstance(terminal, str) else repr(terminal))[:64]
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
            for item in record["attempts"]:
                if "axes" in item:
                    item["axes"]["persisted"] = True
            self._touch(task_id, record)

    def request_record(self, task_id: str, request_id: str) -> dict[str, Any] | None:
        """Read current or archived request metadata without creating a missing root."""
        if not self.path(task_id).parent.is_dir():
            return None
        with self._locked(task_id):
            return self._find_request(task_id, request_id, self.load(task_id))

    def _find_request(self, task_id: str, request_id: str, current) -> dict[str, Any] | None:
        if self._pending_unreadable(task_id, current):
            raise ReconciliationRequired("request history is unreadable; uniqueness cannot be verified")
        if current is not None and current.get("request", {}).get("id") == request_id:
            return current
        rows, _meta = self._archive_rows(task_id, current)
        for row in rows:
            if row["id"] == request_id:
                path = self.path(task_id).with_name(row["file"])
                previous = self._read_archive(task_id, path)
                if self._index_entry(previous, path) != row:
                    raise ReconciliationRequired("request archive changed during lookup")
                return previous
        return None

    def _read_archive(self, task_id, path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            _validate(record, task_id)
            if path.name != f"execution.g{record['generation']}.json" or record["status"] != FINISHED:
                raise ValueError("archive generation/status is inconsistent")
            return record
        except (OSError, ValueError) as exc:
            raise ReconciliationRequired("archived request history is unreadable") from exc

    @staticmethod
    def _index_entry(record, path):
        request = record.get("request", {})
        stat = path.stat()
        # The terminal label is free text (close() and checkpoints do not bound it).
        # Keep index rows well under the 4096-byte-per-row cache guard so a long
        # label cannot force a full archive scan on every lookup.
        terminal = record.get("terminal")
        terminal = terminal[:_INDEX_TERMINAL_CHARS] if isinstance(terminal, str) else None
        return {"id": request.get("id"), "payload_hash": request.get("payload_hash"),
                "generation": record["generation"], "status": record["status"],
                "terminal": terminal, "file": path.name,
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _archive_rows(self, task_id, current):
        """The index is a cache bound to the current journal and archive metadata.

        Archives are immutable through this API. The binding detects accidental
        cache/archive drift; it is not protection from an actor who can rewrite
        the journal, archive contents and filesystem timestamps together.
        """
        directory = self.path(task_id).parent
        archives = {path.name: path for path in directory.glob("execution.g*.json")}
        index = directory / "requests.index.jsonl"
        expected = (current or {}).get("archive_index")
        try:
            if (not isinstance(expected, dict) or type(expected.get("count")) is not int
                    or type(expected.get("bytes")) is not int or not isinstance(expected.get("sha256"), str)
                    or expected.get("count") != len(archives)
                    or index.stat().st_size != expected.get("bytes")
                    or index.stat().st_size > max(1, len(archives)) * 4096):
                raise ValueError("index binding is unavailable")
            data = index.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected.get("sha256") or (data and not data.endswith(b"\n")):
                raise ValueError("index digest is stale")
            rows = [json.loads(line) for line in data.splitlines()]
            if len(rows) != len(archives) or {row["file"] for row in rows} != set(archives):
                raise ValueError("index archive set differs")
            identities = set()
            for row in rows:
                path = archives[row["file"]]
                stat = path.stat()
                if (type(row["generation"]) is not int or row["file"] != f"execution.g{row['generation']}.json"
                        or row["status"] != FINISHED or row["size"] != stat.st_size or row["mtime_ns"] != stat.st_mtime_ns):
                    raise ValueError("archive metadata changed")
                if row["id"] is not None:
                    if (not isinstance(row["id"], str) or not _TASK_ID.fullmatch(row["id"])
                            or not isinstance(row["payload_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["payload_hash"])
                            or row["id"] in identities):
                        raise ValueError("index identity is malformed")
                    identities.add(row["id"])
            meta = expected
        except (OSError, ValueError, KeyError, TypeError):
            rows, identities = [], set()
            for path in sorted(archives.values()):
                record = self._read_archive(task_id, path)
                row = self._index_entry(record, path)
                if row["id"] is not None:
                    if row["id"] in identities:
                        raise ReconciliationRequired("duplicate archived request identity")
                    identities.add(row["id"])
                rows.append(row)
            meta = self._store_index(task_id, rows)
        self._index_state[task_id] = (rows, meta)
        return rows, meta

    def _store_index(self, task_id, rows, *, append=None):
        path = self.path(task_id).with_name("requests.index.jsonl")
        temporary = None
        data = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for row in rows)
        try:
            previous = path.read_bytes() if append is not None else None
            if (previous is not None and len(previous) == append.get("bytes")
                    and hashlib.sha256(previous).hexdigest() == append.get("sha256")
                    and len(rows) == append.get("count", -1) + 1 and data.startswith(previous)):
                with path.open("ab") as output:
                    output.write(data[len(previous):])
                    output.flush()
                    os.fsync(output.fileno())
            else:
                fd, temporary = tempfile.mkstemp(prefix="requests.index.", suffix=".tmp", dir=path.parent)
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                temporary = None
            return {"count": len(rows), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        except OSError:
            return None  # cache failure cannot replace the verified archive scan
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _pending_unreadable(self, task_id, record):
        acknowledged = {item["file"] for item in (record or {}).get("history_acknowledgements", [])}
        return [path for path in self.path(task_id).parent.glob("execution.unreadable-*.json")
                if not path.name.endswith(".acknowledged.json") or path.name not in acknowledged]

    def _next_generation(self, task_id, record=None):
        generations = [int(match.group(1)) for path in self.path(task_id).parent.glob("execution.g*.json")
                       if (match := re.fullmatch(r"execution\.g(\d+)\.json", path.name))]
        return max([record["generation"] if record else 0, *generations]) + 1

    def acknowledge_unreadable(self, task_id: str, note: str) -> dict[str, Any]:
        """Explicit history acknowledgement. Does not settle or resume attempts."""
        if not isinstance(note, str) or not note.strip() or len(note) > 4096:
            raise ValueError("a nonempty acknowledgement note of at most 4096 characters is required")
        directory = self.path(task_id).parent
        if not directory.is_dir():
            raise ValueError("no unreadable history to acknowledge")
        with self._locked(task_id, break_stale=True):
            record = self.load(task_id)  # unreadable current record must be reconciled first
            pending = self._pending_unreadable(task_id, record)
            if not pending:
                raise ValueError("no unreadable history to acknowledge")
            if record and record["status"] == RUNNING and self._owner_alive(record["owner_pid"]):
                raise OwnedElsewhere("live execution owner must stop before acknowledging history")
            now = float(self._wall())
            if record is None:
                record = {"version": 1, "surface": "", "task_id": task_id, "generation": self._next_generation(task_id),
                          "owner": self.owner, "owner_pid": os.getpid(), "status": FINISHED,
                          "started_wall": now, "last_seen_wall": now, "timeout_s": 0.0,
                          "attempts": [], "reconciliations": [], "terminal": "ACKNOWLEDGED_HISTORY"}
            moves = []
            for path in pending:
                target = path if path.name.endswith(".acknowledged.json") else path.with_name(path.stem + ".acknowledged.json")
                if target != path and target.exists():
                    target = path.with_name(path.stem + f"-{uuid.uuid4().hex}.acknowledged.json")
                if target.parent.resolve() != directory.resolve():
                    raise ValueError("acknowledgement must stay in the task directory")
                moves.append((path, target))
                record.setdefault("history_acknowledgements", []).append({
                    "file": target.name, "source": path.name, "note": note.strip(), "at_wall": now, "by_pid": os.getpid()})
            record["request_history_incomplete"] = True
            self._write(task_id, record)  # persist user disposition before moving evidence
            for source, target in moves:
                if source != target:
                    os.rename(source, target)
            return record

    def history_status(self, task_id: str, record=None) -> dict[str, Any]:
        """Read-only counts. Status never repairs an index or creates a directory."""
        archives = list(self.path(task_id).parent.glob("execution.g*.json"))
        return {"archive_files": len(archives), "archive_bytes": sum(path.stat().st_size for path in archives),
                "unacknowledged_files": len(self._pending_unreadable(task_id, record)),
                "request_history_incomplete": bool((record or {}).get("request_history_incomplete")),
                "limitation": "IDs used before history acknowledgement cannot be proven unique" if
                              (record or {}).get("request_history_incomplete") else None}

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
                evidence = path.with_name(f"execution.unreadable-{int(now_wall)}.json")
                if evidence.exists():
                    evidence = path.with_name(f"execution.unreadable-{int(now_wall)}-{uuid.uuid4().hex}.json")
                os.replace(path, evidence)
                record = {"version": _RECORD_VERSION, "surface": "", "task_id": task_id,
                          "generation": self._next_generation(task_id), "owner": self.owner, "owner_pid": os.getpid(),
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
            unresolved = [item for item in record["attempts"]
                          if not item.get("settled") or not item.get("axes", {}).get("persisted")]
            entry["evidence_seen"] = _bounded_evidence({
                "attempts": [{key: item.get(key) for key in ("attempt_id", "axes", "evidence")}
                             for item in unresolved[:50]],
                "unresolved_count": len(unresolved), "truncated": len(unresolved) > 50})
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
    for item in record["attempts"]:
        if "axes" in item:
            _copy_axes(item["axes"])
        if "evidence" in item and len(_json_object(item["evidence"]).encode("utf-8")) > _EVIDENCE_LIMIT:
            raise ValueError("attempt evidence exceeds 16 KiB")
    if "history_acknowledgements" in record:
        acknowledged = record["history_acknowledgements"]
        if not isinstance(acknowledged, list) or record.get("request_history_incomplete") is not True:
            raise ValueError("history acknowledgements are malformed")
        for item in acknowledged:
            if (not isinstance(item, dict) or not isinstance(item.get("file"), str)
                    or not item["file"].startswith("execution.unreadable-")
                    or not item["file"].endswith(".acknowledged.json")
                    or "/" in item["file"] or "\\" in item["file"]
                    or not isinstance(item.get("note"), str) or not item["note"].strip()
                    or len(item["note"]) > 4096 or type(item.get("by_pid")) is not int
                    or type(item.get("at_wall")) not in (int, float) or not math.isfinite(item["at_wall"])):
                raise ValueError("history acknowledgement entry is malformed")
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


def _copy_axes(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        valid = (isinstance(value, dict) and set(value) == {"process", "result", "persisted", "effects"}
                 and all(isinstance(value[key], str) for key in ("process", "result", "effects"))
                 and value["process"] in ("exited", "not_started", "unverified")
                 and value["result"] in ("final", "partial", "none", "error")
                 and type(value["persisted"]) is bool
                 and value["effects"] in ("none_observed", "changed", "unknown"))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("attempt axes are malformed")
    return dict(value)


def _json_object(value: Mapping[str, Any]) -> str:
    if not isinstance(value, dict):
        raise ValueError("attempt evidence must be an object")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False)
        data.encode("utf-8")
        return data
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("attempt evidence must be finite JSON") from exc


def _bounded_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a finite, bounded observation. Truncation never changes admission."""
    data = _json_object(value)
    size = len(data.encode("utf-8"))
    if size <= _EVIDENCE_LIMIT:
        return json.loads(data)
    original = json.loads(data)

    def shrink(item, count, width, depth=0):
        if isinstance(item, str):
            return item[:width]
        if isinstance(item, list):
            return [shrink(child, count, width, depth + 1) for child in item[:count]] if depth < 6 else []
        if isinstance(item, dict):
            if depth >= 6:
                return {}
            return {key: shrink(child, count, width, depth + 1)
                    for key, child in list(item.items())[:count]}
        return item

    count, width = 50, 512
    while True:
        result = shrink(original, count, width)
        result.update(truncated=True, original_bytes=size)
        if len(_json_object(result).encode("utf-8")) <= _EVIDENCE_LIMIT:
            return result
        if count == 1 and width == 1:
            return {"truncated": True, "original_bytes": size}
        count, width = max(1, count // 2), max(1, width // 2)
