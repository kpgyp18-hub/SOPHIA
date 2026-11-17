"""Patch 04: host-owned bounded long tasks. Real detached watchers and children, short durations."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scripts.supervisor import long_task
from scripts.supervisor.long_task import LongTaskError, LongTaskHost, LongTasksDisabled, pid_alive

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable


def _wait_finished(host: LongTaskHost, task_id: str, seconds: float = 20.0) -> dict:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        state = host.status(task_id)
        if state["status"] != "RUNNING":
            return state
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} still running after {seconds}s: {host.status(task_id)}")


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {long_task.FLAG_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = LongTaskHost(Path(self._tmp.name) / "lt")

    def _cleanup_task(self, task_id: str) -> None:
        def run() -> None:
            try:
                state = self.host.status(task_id)
                if state["status"] == "RUNNING":
                    self.host.cancel(task_id)
            except LongTaskError:
                pass
        self.addCleanup(run)


class FlagAndValidationTests(_Base):
    def test_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(long_task.FLAG_ENV, None)
            with self.assertRaises(LongTasksDisabled):
                self.host.start("a", [PY, "-c", "pass"], deadline_s=5)
        self.assertFalse((Path(self._tmp.name) / "lt").exists())  # nothing was created

    def test_rejects_unbounded_or_malformed_requests(self) -> None:
        cmd = [PY, "-c", "pass"]
        for task_id, command, deadline in (("bad id!", cmd, 5), ("../x", cmd, 5), ("ok", [], 5),
                                           ("ok", cmd, 0), ("ok", cmd, -1), ("ok", cmd, 4 * 3600 + 1)):
            with self.assertRaises(LongTaskError, msg=(task_id, command, deadline)):
                self.host.start(task_id, command, deadline_s=deadline)

    def test_existing_task_dir_is_never_overwritten(self) -> None:
        self.host.start("same", [PY, "-c", "print(1)"], deadline_s=20)
        _wait_finished(self.host, "same")
        with self.assertRaises(FileExistsError):
            self.host.start("same", [PY, "-c", "print(2)"], deadline_s=20)
        self.assertIn("1", self.host.collect("same")["stdout_tail"])

    def test_concurrency_is_bounded(self) -> None:
        self.host.start("one", [PY, "-c", "import time; time.sleep(60)"], deadline_s=60)
        self._cleanup_task("one")
        with self.assertRaisesRegex(LongTaskError, "concurrency"):
            self.host.start("two", [PY, "-c", "pass"], deadline_s=5)


class OutcomeTests(_Base):
    def test_completed_task_is_collected_with_output_and_exit_code(self) -> None:
        self.host.start("ok", [PY, "-c", "print('hello long task')"], deadline_s=20)
        state = _wait_finished(self.host, "ok")
        self.assertEqual((state["outcome"], state["exit_code"], state["cleanup"]), ("completed", 0, "child_gone"))
        result = self.host.collect("ok")
        self.assertTrue(result["finished"])
        self.assertIn("hello long task", result["stdout_tail"])

    def test_nonzero_exit_is_reported_not_hidden(self) -> None:
        self.host.start("bad", [PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], deadline_s=20)
        state = _wait_finished(self.host, "bad")
        self.assertEqual((state["outcome"], state["exit_code"]), ("nonzero_exit", 3))
        self.assertIn("boom", self.host.collect("bad")["stderr_tail"])

    def test_deadline_kills_the_whole_tree(self) -> None:
        pidfile = Path(self._tmp.name) / "grandchild.pid"
        grandchild = "import os, time; open(" + repr(str(pidfile)) + ", 'w').write(str(os.getpid())); time.sleep(120)"
        parent = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', " + repr(grandchild) + "])\n"
                  "time.sleep(120)\n")
        started = self.host.start("slow", [PY, "-c", parent], deadline_s=2.5)
        child_pid = started["child_pid"]
        state = _wait_finished(self.host, "slow")
        self.assertEqual((state["outcome"], state["cleanup"]), ("timeout", "child_gone"))
        self.assertTrue(_wait_gone(child_pid), "child survived the deadline")
        end = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(0.1)
        self.assertTrue(pidfile.exists(), "grandchild never started; test is vacuous")
        self.assertTrue(_wait_gone(int(pidfile.read_text())), "grandchild survived the tree kill")

    def test_cancel_stops_a_running_task(self) -> None:
        started = self.host.start("cx", [PY, "-c", "import time; time.sleep(120)"], deadline_s=120)
        self._cleanup_task("cx")
        final = self.host.cancel("cx")
        self.assertEqual((final["status"], final["outcome"], final["cleanup"]), ("FINISHED", "cancelled", "child_gone"))
        self.assertTrue(_wait_gone(started["child_pid"]))

    def test_unlaunchable_command_is_an_error_outcome(self) -> None:
        self.host.start("nope", ["definitely-not-a-real-binary-xyz"], deadline_s=20)
        state = _wait_finished(self.host, "nope")
        self.assertEqual(state["outcome"], "error")
        self.assertIn("error", state)


class DetachmentTests(_Base):
    def test_task_outlives_its_launcher_and_is_collected_by_a_new_host(self) -> None:
        """The launcher stands in for a chat turn / relay that exits right after starting the job."""
        marker = Path(self._tmp.name) / "done.txt"
        job = "import time; time.sleep(2); open(" + repr(str(marker)) + ", 'w').write('finished')"
        launcher = (
            "from scripts.supervisor.long_task import LongTaskHost\n"
            "h = LongTaskHost(" + repr(str(Path(self._tmp.name) / "lt")) + ")\n"
            "h.start('detached', [" + repr(PY) + ", '-c', " + repr(job) + "], deadline_s=60)\n"
        )
        env = dict(os.environ, **{long_task.FLAG_ENV: "1"})
        done = subprocess.run([PY, "-c", launcher], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self._cleanup_task("detached")
        # launcher is gone; the job must still be running under the watcher, then finish.
        self.assertFalse(marker.exists())
        fresh = LongTaskHost(Path(self._tmp.name) / "lt")
        state = _wait_finished(fresh, "detached")
        self.assertEqual((state["outcome"], state["exit_code"]), ("completed", 0))
        self.assertEqual(marker.read_text(), "finished")


class LostWatcherTests(_Base):
    def _fake_running_state(self, task_id: str, watcher_pid: int, child_pid: int | None) -> Path:
        task_dir = self.host.root / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "state.json").write_text(json.dumps({
            "task_id": task_id, "status": "RUNNING", "outcome": None, "exit_code": None,
            "watcher_pid": watcher_pid, "child_pid": child_pid, "started_at": "x"}), encoding="utf-8")
        return task_dir

    def test_dead_watcher_without_outcome_is_reported_lost_not_guessed(self) -> None:
        dead = subprocess.Popen([PY, "-c", "pass"])
        dead.wait()
        self._fake_running_state("lost", dead.pid, None)
        state = self.host.status("lost")
        self.assertEqual((state["status"], state["outcome"]), ("LOST", "lost"))
        self.assertTrue(self.host.collect("lost")["finished"])

    def test_cancel_of_a_lost_task_reaps_the_orphaned_child(self) -> None:
        dead = subprocess.Popen([PY, "-c", "pass"])
        dead.wait()
        orphan = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: orphan.poll() is None and orphan.kill())
        self._fake_running_state("orph", dead.pid, orphan.pid)
        self.assertTrue(self.host.status("orph")["child_still_running"])
        final = self.host.cancel("orph")
        self.assertEqual((final["status"], final["outcome"], final["cleanup"]), ("FINISHED", "cancelled", "child_gone"))
        self.assertTrue(_wait_gone(orphan.pid))


class AdmissionRegressionTests(_Base):
    class NoSpawnHost(LongTaskHost):
        @staticmethod
        def _spawn_watcher(task_dir):
            state = long_task._read_state(task_dir)
            state.update(watcher_pid=os.getpid(), child_pid=os.getpid())
            long_task._write_state(task_dir, state)
            return os.getpid()

    def test_two_threads_cannot_both_reserve_the_last_slot(self):
        gate = threading.Barrier(2)
        def start(index):
            gate.wait(timeout=5)
            try:
                self.NoSpawnHost(self.host.root).start(f"fake-{index}", ["NEVER_EXECUTED"], deadline_s=5)
                return True
            except LongTaskError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = list(pool.map(start, (1, 2)))
        self.assertEqual(sum(admitted), 1)

    def test_two_processes_cannot_both_reserve_the_last_slot(self):
        control = Path(self._tmp.name)
        code = """
import json,os,sys,time
from pathlib import Path
from scripts.supervisor.long_task import LongTaskHost,LongTaskError,_read_state,_write_state
class Host(LongTaskHost):
 @staticmethod
 def _spawn_watcher(directory):
  state=_read_state(directory);state.update(watcher_pid=os.getpid(),child_pid=os.getpid());_write_state(directory,state)
  return os.getpid()
control=Path(sys.argv[1]); index=sys.argv[2]
(control/('ready-'+index)).touch()
until=time.monotonic()+10
while not (control/'go').exists() and time.monotonic()<until: time.sleep(.01)
try:
 Host(control/'lt').start('fake-'+index,['NEVER_EXECUTED'],deadline_s=5)
 admitted=True
except LongTaskError: admitted=False
(control/('result-'+index)).write_text(json.dumps(admitted))
while not (control/'release').exists() and time.monotonic()<until: time.sleep(.01)
"""
        processes = [subprocess.Popen([PY, "-c", code, str(control), str(i)], cwd=REPO_ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in (1, 2)]
        try:
            until = time.monotonic() + 7
            while not all((control / f"ready-{i}").exists() for i in (1, 2)) and time.monotonic() < until:
                time.sleep(.02)
            self.assertTrue(all((control / f"ready-{i}").exists() for i in (1, 2)))
            (control / "go").touch()
            while not all((control / f"result-{i}").exists() for i in (1, 2)) and time.monotonic() < until:
                time.sleep(.02)
            self.assertTrue(all((control / f"result-{i}").exists() for i in (1, 2)))
            self.assertEqual(sum(json.loads((control / f"result-{i}").read_text()) for i in (1, 2)), 1)
        finally:
            (control / "release").touch()
            for process in processes:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

    def test_admission_os_lock_is_released_when_owner_dies(self):
        code = ("import os,sys\nfrom pathlib import Path\n"
                "from scripts.supervisor.long_task import _admission\n"
                "with _admission(Path(sys.argv[1])): os._exit(7)\n")
        result = subprocess.run([PY, "-c", code, str(self.host.root)], cwd=REPO_ROOT,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.NoSpawnHost(self.host.root).start("after-death", ["NEVER_EXECUTED"], deadline_s=5)

    def test_lost_unconfirmed_and_unreadable_records_block_new_work(self):
        for kind in ("lost", "unconfirmed", "unreadable", "unknown"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                host = self.NoSpawnHost(tmp)
                directory = Path(tmp) / "old"
                directory.mkdir()
                state = {"task_id": "old", "status": "LOST", "child_pid": os.getpid()}
                if kind == "unconfirmed":
                    state.update(status="FINISHED", cleanup="unconfirmed")
                elif kind == "unknown":
                    state["status"] = "UNKNOWN"
                (directory / "state.json").write_text("{broken" if kind == "unreadable" else json.dumps(state))
                with self.assertRaises(LongTaskError):
                    host.start("next", ["NEVER_EXECUTED"], deadline_s=5)
                self.assertFalse((Path(tmp) / "next").exists())

    def test_reconcile_requires_note_and_refuses_live_process(self):
        directory = self.host.root / "old"
        directory.mkdir(parents=True)
        long_task._write_state(directory, {"task_id": "old", "status": "LOST", "child_pid": os.getpid()})
        with self.assertRaises(LongTaskError):
            self.host.reconcile("old", note="")
        with self.assertRaisesRegex(LongTaskError, "still alive"):
            self.host.reconcile("old", note="inspect")
        long_task._write_state(directory, {"task_id": "old", "status": "LOST", "child_pid": None})
        final = self.host.reconcile("old", note="verified child termination and effects")
        self.assertEqual(final["cleanup"], "operator_verified")
        self.NoSpawnHost(self.host.root).start("next", ["NEVER_EXECUTED"], deadline_s=5)

    def test_reconcile_preserves_unreadable_evidence(self):
        directory = self.host.root / "old"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text("{broken")
        self.host.reconcile("old", note="manually verified no child or effects")
        evidence = list(directory.glob("state.before-reconcile-*.json"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].read_text(), "{broken")

    def test_watcher_spawn_failure_does_not_leave_a_false_running_reservation(self):
        with mock.patch.object(self.host, "_spawn_watcher", side_effect=OSError("offline fixture")):
            with self.assertRaisesRegex(LongTaskError, "watcher did not start"):
                self.host.start("failed", ["NEVER_EXECUTED"], deadline_s=5)
        self.assertEqual(self.host.status("failed")["cleanup"], "child_gone")
        self.NoSpawnHost(self.host.root).start("next", ["NEVER_EXECUTED"], deadline_s=5)

    def test_terminal_write_racing_with_watcher_exit_is_not_reported_lost(self):
        running = {"task_id": "race", "status": "RUNNING", "watcher_pid": 999999, "child_pid": None}
        finished = dict(running, status="FINISHED", outcome="completed", cleanup="child_gone")
        with mock.patch.object(long_task, "_read_state", side_effect=[running, finished]), mock.patch.object(
                long_task, "pid_alive", return_value=False):
            self.assertEqual(self.host.status("race"), finished)

    def test_open_state_snapshot_does_not_block_atomic_replacement(self):
        directory = self.host.root / "snapshot"
        directory.mkdir(parents=True)
        long_task._write_state(directory, {"status": "RUNNING"})
        with long_task._open_state(directory / "state.json") as reader:
            long_task._write_state(directory, {"status": "FINISHED"})
            self.assertEqual(json.load(reader)["status"], "RUNNING")
        self.assertEqual(long_task._read_state(directory)["status"], "FINISHED")


if __name__ == "__main__":
    unittest.main()
