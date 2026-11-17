"""G2 C restart recovery, wired into the Supervisor behind SOPHIA_G2_DEADLINE_CONTRACT."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.supervisor.call_graph import CLAUDE_CODEX, DIRECT_CODEX
from scripts.supervisor.claude_adapter import ClaudeCLIAdapter
from scripts.supervisor.codex_adapter import CodexCLIAdapter
from scripts.supervisor.model_budget import ModelBudget
from scripts.supervisor.model_router import ModelRouter
from scripts.supervisor.supervisor import Supervisor
from sophia.runtime.execution_recovery import (FINISHED, NEEDS_RECONCILIATION, RUNNING,
                                               ExecutionJournal)

ROOT = Path(__file__).resolve().parents[2]
FLAG = "SOPHIA_G2_DEADLINE_CONTRACT"
OK_EXEC = '{"status":"OK","step_id":"01-exec"}'
HANDOFF = ('{"status":"OK","step_id":"01-plan","handoff_request":{"target_agent":"CODEX",'
           '"reason_code":"IMPLEMENTATION_REQUIRED","required_files":[],"required_skills":[],'
           '"return_to_agent":"NONE"}}')
ROUTING = {"*": {"complexity": "normal", "requires_reasoning": True, "task_type": "structured"}}


class EventRunner:
    """A runner double that reports lifecycle events the way the real Runner does."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float | None]] = []
        self.last_events: dict[str, float] = {}

    def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None,
                    deadline_at=None):
        self.calls.append((task_id, timeout))
        response = self.responses.pop(0)
        now = time.monotonic()
        if isinstance(response, BaseException):
            self.last_events = {"process_start": now, "timeout": now, "cleanup_end": now}
            raise response
        self.last_events = {"process_start": now, "child_exit": now}
        return response


def codex(runner, timeout_seconds: float = 100.0) -> CodexCLIAdapter:
    return CodexCLIAdapter(runner, timeout_seconds=timeout_seconds, executable_path="mock-codex",
                           show_progress=False)


def supervisor(tmp_path: Path, adapters, **kwargs) -> Supervisor:
    return Supervisor(tmp_path / "runtime", tmp_path / "skills", adapters, **kwargs)


def record(tmp_path: Path, task_id: str) -> dict:
    return json.loads((tmp_path / "runtime" / task_id / "execution.json").read_text(encoding="utf-8"))


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "1")


@pytest.fixture
def dead_owner(monkeypatch):
    """Every earlier owner is gone -- what a restart after a crash looks like."""
    monkeypatch.setattr("scripts.supervisor.long_task.pid_alive", lambda pid: False)


def test_default_path_writes_no_execution_record(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    runner = EventRunner([(0, OK_EXEC, "")])
    state = supervisor(tmp_path, {"CODEX": codex(runner)}).run("task-off", DIRECT_CODEX)
    assert state["status"] == "DONE"
    assert not (tmp_path / "runtime" / "task-off" / "execution.json").exists()


def test_completed_run_leaves_a_finished_record(tmp_path, flag_on) -> None:
    runner = EventRunner([(0, OK_EXEC, "")])
    state = supervisor(tmp_path, {"CODEX": codex(runner)}).run("task-ok", DIRECT_CODEX)
    saved = record(tmp_path, "task-ok")
    assert (state["status"], saved["status"], saved["terminal"]) == ("DONE", FINISHED, "DONE")
    assert [(item["label"], item["settled"], item["detail"]) for item in saved["attempts"]] == [
        ("01-exec", True, "completed")]


def test_timed_out_task_is_not_reexecuted_until_reconciled(tmp_path, flag_on, dead_owner,
                                                           monkeypatch) -> None:
    first = EventRunner([subprocess.TimeoutExpired("codex", 100)])
    state = supervisor(tmp_path, {"CODEX": codex(first)}).run("task-timeout", DIRECT_CODEX)
    saved = record(tmp_path, "task-timeout")
    assert (state["status"], saved["status"], saved["terminal"]) == (
        "FAILED", NEEDS_RECONCILIATION, "FAILED")
    assert saved["attempts"][0]["settled"] is False

    # Re-running the same task -- by a person, a wrapper or a restart loop -- launches nothing.
    retry = EventRunner([(0, OK_EXEC, "")])
    budget = ModelBudget()
    for _ in range(2):
        blocked = supervisor(tmp_path, {"CODEX": codex(retry)}, model_router=ModelRouter(),
                             model_budget=budget, codex_feature_inputs=ROUTING).run(
            "task-timeout", DIRECT_CODEX)
        assert blocked["status"] == "NEED_USER"
    assert retry.calls == []
    assert (budget.luna_calls, budget.terra_calls, budget.sol_calls) == (0, 0, 0)
    stored = json.loads((tmp_path / "runtime" / "task-timeout" / "state.json").read_text("utf-8"))
    assert stored["status"] == "FAILED"          # the earlier run's own verdict is kept

    monkeypatch.setattr(sys, "argv", ["agents.py", "--reconcile", "task-timeout",
                                      "--note", "no codex process left; workspace unchanged",
                                      "--runtime-root", str(tmp_path / "runtime")])
    from scripts import agents
    assert agents.main() == 0
    released = supervisor(tmp_path, {"CODEX": codex(retry)}).run("task-timeout", DIRECT_CODEX)
    assert released["status"] == "DONE" and len(retry.calls) == 1
    assert record(tmp_path, "task-timeout")["generation"] == 2
    kept = json.loads((tmp_path / "runtime" / "task-timeout" / "execution.g1.json").read_text("utf-8"))
    assert kept["reconciliations"][0]["note"] == "no codex process left; workspace unchanged"


def test_reconcile_cli_requires_a_note_and_reports_refusal(tmp_path, monkeypatch, capsys) -> None:
    from scripts import agents
    base = ["agents.py", "--runtime-root", str(tmp_path / "runtime"), "--reconcile", "task-none"]
    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit):
        agents.main()
    monkeypatch.setattr(sys, "argv", [*base, "--note", "checked"])
    assert agents.main() == 1
    assert "reconcile refused: no execution record" in capsys.readouterr().out


def test_restart_before_any_attempt_inherits_the_remaining_budget(tmp_path, flag_on,
                                                                  dead_owner) -> None:
    # An earlier owner opened a 100s budget 60s ago and died before launching anything.
    earlier = ExecutionJournal(tmp_path / "runtime", wall_clock=lambda: time.time() - 60)
    earlier.open("agent_supervisor", "task-late", "a0", 100)
    runner = EventRunner([(0, OK_EXEC, "")])
    state = supervisor(tmp_path, {"CODEX": codex(runner, 100.0)}).run("task-late", DIRECT_CODEX)
    assert state["status"] == "DONE"
    granted = runner.calls[0][1]
    assert 30 < granted <= 40                      # what was left, not a new 100s
    saved = record(tmp_path, "task-late")
    assert (saved["generation"], saved["takeovers"], saved["status"]) == (1, 1, FINISHED)


def test_restart_after_the_deadline_launches_nothing(tmp_path, flag_on, dead_owner) -> None:
    earlier = ExecutionJournal(tmp_path / "runtime", wall_clock=lambda: time.time() - 500)
    earlier.open("agent_supervisor", "task-expired", "a0", 100)
    runner = EventRunner([(0, OK_EXEC, "")])
    state = supervisor(tmp_path, {"CODEX": codex(runner, 100.0)}).run("task-expired", DIRECT_CODEX)
    assert state["status"] == "NEED_USER" and runner.calls == []
    saved = record(tmp_path, "task-expired")
    assert (saved["status"], saved["terminal"]) == (FINISHED, "EXPIRED")


def test_task_held_by_a_live_owner_is_refused_without_touching_its_state(tmp_path, flag_on) -> None:
    holder = ExecutionJournal(tmp_path / "runtime")
    holder.open("agent_supervisor", "task-held", "a0", 100)        # owner pid = this live process
    state_path = tmp_path / "runtime" / "task-held" / "state.json"
    state_path.write_text(json.dumps({"task_id": "task-held", "status": "RUNNING",
                                      "step_number": 1}), encoding="utf-8")
    before = (state_path.read_text("utf-8"), record(tmp_path, "task-held"))
    runner = EventRunner([(0, OK_EXEC, "")])
    state = supervisor(tmp_path, {"CODEX": codex(runner)}).run("task-held", DIRECT_CODEX)
    assert state["status"] == "NEED_USER" and runner.calls == []
    assert (state_path.read_text("utf-8"), record(tmp_path, "task-held")) == before


def test_running_state_without_a_record_is_treated_as_interrupted(tmp_path, flag_on) -> None:
    # A run that died before this contract existed (or with the flag off).
    state_path = tmp_path / "runtime" / "task-old" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"task_id": "task-old", "status": "RUNNING",
                                      "current_agent": "CODEX", "step_number": 2,
                                      "agent_history": ["CLAUDE", "CODEX"]}), encoding="utf-8")
    runner = EventRunner([(0, OK_EXEC, "")])
    state = supervisor(tmp_path, {"CODEX": codex(runner)}).run("task-old", DIRECT_CODEX)
    assert state["status"] == "NEED_USER" and runner.calls == []
    stored = json.loads(state_path.read_text("utf-8"))
    assert (stored["status"], stored["step_number"], stored["agent_history"]) == (
        "NEED_USER", 2, ["CLAUDE", "CODEX"])
    assert record(tmp_path, "task-old")["status"] == NEEDS_RECONCILIATION


_CRASHING_OWNER = """
import os, sys, time
from pathlib import Path
from scripts.supervisor.call_graph import CLAUDE_CODEX
from scripts.supervisor.claude_adapter import ClaudeCLIAdapter
from scripts.supervisor.codex_adapter import CodexCLIAdapter
from scripts.supervisor.supervisor import Supervisor

class Runner:
    last_events = {}
    def __init__(self, reply): self.reply = reply
    def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None,
                    deadline_at=None):
        if self.reply is None:
            os._exit(7)      # the host dies while the second step's CLI is running
        now = time.monotonic()
        self.last_events = {"process_start": now, "child_exit": now}
        return 0, self.reply, ""

root = Path(sys.argv[1])
adapters = {"CLAUDE": ClaudeCLIAdapter(Runner(sys.argv[2]), timeout_seconds=100,
                                       executable_path="mock", show_progress=False),
            "CODEX": CodexCLIAdapter(Runner(None), timeout_seconds=100,
                                     executable_path="mock", show_progress=False)}
Supervisor(root / "runtime", root / "skills", adapters).run("task-crash", CLAUDE_CODEX)
"""


def test_host_killed_mid_step_blocks_the_task_after_a_real_restart(tmp_path, flag_on) -> None:
    """A real process owns the task and dies; liveness is checked against its real pid."""
    child = subprocess.run([sys.executable, "-c", _CRASHING_OWNER, str(tmp_path), HANDOFF],
                           cwd=ROOT, env={**os.environ, FLAG: "1"}, capture_output=True,
                           text=True, timeout=60)
    assert child.returncode == 7, child.stderr
    left = record(tmp_path, "task-crash")
    assert (left["status"], left["owner_pid"] != os.getpid()) == (RUNNING, True)
    assert [(item["label"], item["settled"]) for item in left["attempts"]] == [
        ("01-plan", True), ("02-exec", False)]

    claude_runner, codex_runner = EventRunner([(0, HANDOFF, "")]), EventRunner([(0, OK_EXEC, "")])
    budget = ModelBudget()
    restarted = supervisor(
        tmp_path, {"CLAUDE": ClaudeCLIAdapter(claude_runner, timeout_seconds=100,
                                              executable_path="mock", show_progress=False),
                   "CODEX": codex(codex_runner)},
        model_router=ModelRouter(), model_budget=budget, codex_feature_inputs=ROUTING)
    state = restarted.run("task-crash", CLAUDE_CODEX)
    assert state["status"] == "NEED_USER"
    assert (claude_runner.calls, codex_runner.calls) == ([], [])
    assert (budget.luna_calls, budget.terra_calls, budget.sol_calls) == (0, 0, 0)
    after = record(tmp_path, "task-crash")
    assert after["status"] == NEEDS_RECONCILIATION and "2 started attempt" in after["reason"]
    # The interrupted step stays on record; only the stale RUNNING status is corrected.
    assert (state["current_agent"], state["step_number"]) == ("CODEX", 2)
