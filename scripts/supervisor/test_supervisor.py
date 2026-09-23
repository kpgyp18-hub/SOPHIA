import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.supervisor.call_graph import (CLAUDE_CODEX, CLAUDE_CODEX_REVIEW, CLAUDE_ONLY,
                                            DIRECT_CODEX, EXCEPTION_GRAPH, validate_call_graph,
                                            validate_call_graph_prefix)
from scripts.supervisor.loop_guard import LoopGuard
from scripts.supervisor.presentation import emit_done, emit_handoff, emit_running, emit_start
from scripts.supervisor.runner import Runner
from scripts.supervisor.runtime_store import RuntimeStore
from scripts.supervisor.schema import (SchemaValidationError, normalize_structured_out_json, out_json_schema, structured_out_json_schema, validate_handoff_request,
                                       validate_out_json, validate_required_skills)
from scripts.supervisor.writer_lock import WriterBusyError, WriterLock
from scripts.supervisor.supervisor import Supervisor, _dry_run_adapters
from scripts.supervisor.claude_adapter import ClaudeCLIAdapter
from scripts.supervisor.codex_adapter import CodexCLIAdapter
from scripts.supervisor.cli_adapter import CLIAdapterError
from unittest.mock import patch
import subprocess


def handoff(**changes):
    value = {"target_agent": "CODEX", "reason_code": "IMPLEMENTATION_REQUIRED",
             "required_files": ["docs/input.md"], "required_skills": [], "return_to_agent": "NONE"}
    value.update(changes)
    return value


class FakeProcess:
    def __init__(self, running=True, terminate_exits=True, pid=None):
        self.running = running
        self.terminate_exits = terminate_exits
        self.terminated = False
        self.killed = False
        self.pid = pid

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        if self.running:
            import subprocess
            raise subprocess.TimeoutExpired("agent", timeout)
        return 0

    def terminate(self):
        self.terminated = True
        if self.terminate_exits:
            self.running = False

    def kill(self):
        self.killed = True
        self.running = False


class SupervisorRequirementsTests(unittest.TestCase):
    # 1. Direct execution of the peer CLI is blocked by project execpolicy.
    def test_execpolicy_blocks_claude_binaries(self):
        rules = Path("supervisor.execpolicy.rules")
        self.assertTrue(rules.is_file())
        text = rules.read_text(encoding="utf-8")
        self.assertIn('prefix_rule(pattern = ["claude"], decision = "forbidden"', text)
        self.assertIn('prefix_rule(pattern = ["claude.exe"], decision = "forbidden"', text)

    # 2. Required and unknown machine-protocol fields are rejected.
    def test_schema_rejects_missing_and_unknown_out_fields(self):
        with self.assertRaises(SchemaValidationError):
            validate_out_json({"status": "OK"})
        with self.assertRaises(SchemaValidationError):
            validate_out_json({"status": "OK", "step_id": "01", "persona": "no"})
        with self.assertRaises(SchemaValidationError):
            validate_handoff_request(handoff(persona="no"))

    # 3. Each loop budget reaches a terminal protocol status.
    def test_loop_limits_return_terminal_statuses(self):
        guard = LoopGuard()
        self.assertEqual(guard.record_handoff("CODEX", "A"), (True, None))
        self.assertEqual(guard.record_handoff("CLAUDE", "B"), (True, None))
        self.assertEqual(guard.record_handoff("CODEX", "C"), (True, None))
        self.assertEqual(guard.record_handoff("CLAUDE", "D"), (True, None))
        self.assertEqual(guard.record_handoff("CODEX", "E"), (False, "NEED_USER"))
        same = LoopGuard()
        same.record_handoff("CODEX", "A")
        same.record_handoff("CLAUDE", "A")
        self.assertEqual(same.record_handoff("CODEX", "A"), (False, "FAILED"))
        rounds = LoopGuard(agent_history=["CLAUDE", "CODEX", "CLAUDE", "CODEX", "CLAUDE", "CODEX", "CLAUDE"], cross_agent_roundtrips=5)
        self.assertEqual(rounds.record_handoff("CODEX", "Z"), (False, "NEED_USER"))
        exception_graph = LoopGuard(current_agent="CLAUDE")
        for target, reason in (("CODEX", "IMPLEMENTATION_REQUIRED"), ("CLAUDE", "RISK_REVIEW_REQUIRED"),
                                ("CODEX", "DEBUG_REQUIRED"), ("CLAUDE", "DESIGN_REQUIRED")):
            self.assertEqual(exception_graph.record_handoff(target, reason), (True, None))
        self.assertEqual(tuple(exception_graph.agent_history), EXCEPTION_GRAPH)

    # 4. A writer cannot start until its predecessor is confirmed stopped.
    def test_writer_lock_requires_previous_process_exit(self):
        lock, first = WriterLock(), FakeProcess()
        lock.acquire("task-1", first)
        self.assertFalse(lock.can_start_next("task-1"))
        with self.assertRaises(WriterBusyError):
            lock.acquire("task-1", FakeProcess())
        first.running = False
        self.assertTrue(lock.can_start_next("task-1"))

    # 5. Four normal routes plus the one named exception are the complete allowlist.
    def test_call_graph_allowlist(self):
        for route in (DIRECT_CODEX, CLAUDE_ONLY, CLAUDE_CODEX, CLAUDE_CODEX_REVIEW, EXCEPTION_GRAPH):
            self.assertEqual(validate_call_graph(route), (True, None))
        self.assertEqual(validate_call_graph(["CODEX", "CLAUDE"]), (False, "NEED_USER"))

    # 6. A handoff cannot reference paths outside its task root.
    def test_required_files_must_stay_under_task_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SchemaValidationError):
                validate_handoff_request(handoff(required_files=["../private.txt"]), task_root=directory)
            with self.assertRaises(SchemaValidationError):
                validate_handoff_request(handoff(required_files=[str(Path(directory).resolve())]), task_root=directory)

    # 7. Missing Skill IDs are surfaced for the caller to remove or fail explicitly.
    def test_missing_required_skill_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "present").mkdir()
            self.assertEqual(validate_required_skills(["present", "missing"], root), ["missing"])

    # 8. The starting agent is included in roundtrip counting.
    def test_initial_agent_roundtrip_count(self):
        guard = LoopGuard(current_agent="CLAUDE")
        self.assertEqual(guard.record_handoff("CODEX", "A"), (True, None))
        self.assertEqual(guard.record_handoff("CLAUDE", "B"), (True, None))
        self.assertEqual(guard.cross_agent_roundtrips, 1)

    # 9. Runtime paths reject unsafe task IDs.
    def test_runtime_store_rejects_task_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(directory)
            for task_id in ("../evil", "..\\evil", "C:\\evil", "/evil"):
                with self.assertRaises(ValueError):
                    store.step_path(task_id, 1, "exec", "task")

    # 10. Runtime paths reject unsafe roles.
    def test_runtime_store_rejects_role_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(directory)
            for role in ("../review", "exec/../../evil", "C:\\temp"):
                with self.assertRaises(ValueError):
                    store.step_path("task-1", 1, role, "task")

    # 11. Skill IDs are identifiers, never filesystem paths.
    def test_required_skills_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            for skill_id in ("../secret", "..\\secret", "/absolute", "code/review"):
                with self.assertRaises(SchemaValidationError):
                    validate_required_skills([skill_id], directory)

    # 12. Timeout cleanup releases the writer lock after terminate/kill escalation.
    def test_runner_timeout_cleans_up_writer_lock(self):
        lock, process = WriterLock(), FakeProcess(terminate_exits=False)
        lock.acquire("task-1", process)
        with self.assertRaises(Exception):
            Runner(lock).wait("task-1", process, timeout=0.01)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(lock.can_start_next("task-1"))

    # 12b. On Windows, timeout escalation also reaps the whole process tree via taskkill
    # (process.terminate()/kill() alone only signal the direct child; CLI wrappers like
    # claude.cmd/codex.cmd spawn further processes that would otherwise be orphaned).
    def test_runner_timeout_kills_windows_process_tree(self):
        from scripts.supervisor import runner as runner_module
        process = FakeProcess(terminate_exits=False, pid=4321)
        with patch.object(runner_module.os, "name", "nt"), patch.object(runner_module.subprocess, "run") as mock_run:
            with self.assertRaises(Exception):
                Runner(WriterLock()).wait("task-tree", process, timeout=0.01)
        from unittest.mock import call
        self.assertEqual(mock_run.call_args_list, [
            call(["taskkill", "/T", "/PID", "4321"], capture_output=True, check=False),
            call(["taskkill", "/F", "/T", "/PID", "4321"], capture_output=True, check=False),
        ])

    # 12c. On non-Windows or when the process object has no pid (e.g. FakeProcess in
    # most tests), no taskkill is attempted; behavior stays exactly as before.
    def test_runner_timeout_skips_process_tree_kill_when_not_windows_or_no_pid(self):
        from scripts.supervisor import runner as runner_module
        with patch.object(runner_module.subprocess, "run") as mock_run:
            with self.assertRaises(Exception):
                Runner(WriterLock()).wait("task-nopid", FakeProcess(terminate_exits=False), timeout=0.01)
        mock_run.assert_not_called()

    # 13. Presentation emits stable foreground status prefixes.
    def test_presentation_output(self):
        output = io.StringIO()
        with redirect_stdout(output):
            emit_start("CLAUDE", "plan")
            emit_handoff("CLAUDE", "CODEX", "IMPLEMENTATION_REQUIRED")
            emit_done()
        self.assertIn("[START] CLAUDE -> plan", output.getvalue())
        self.assertIn("[HANDOFF -> CODEX] IMPLEMENTATION_REQUIRED", output.getvalue())
        self.assertIn("[DONE] Task completed", output.getvalue())

    def test_presentation_step_progress_and_legacy_format(self):
        output = io.StringIO()
        with redirect_stdout(output):
            emit_running("CODEX", "exec", step=2, total=3)
            emit_running("CODEX", "exec")
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "[RUNNING] Step 2/3 CODEX -> exec")
        self.assertEqual(lines[1], "[RUNNING] CODEX -> exec")

    # 14. Human-facing Persona strings cannot enter strict machine JSON.
    def test_machine_json_rejects_persona_fields(self):
        with self.assertRaises(SchemaValidationError):
            validate_out_json({"status": "OK", "step_id": "01", "presentation": "I have Control!"})

    # 15. Reason codes must match the current handoff direction.
    def test_reason_direction_validation(self):
        validate_handoff_request(handoff(), current_agent="CLAUDE")
        with self.assertRaises(SchemaValidationError):
            validate_handoff_request(handoff(reason_code="DESIGN_REQUIRED"), current_agent="CLAUDE")
        validate_handoff_request(handoff(target_agent="CLAUDE", reason_code="DESIGN_REQUIRED"), current_agent="CODEX")

    # 16. In-progress call graphs accept only known route prefixes.
    def test_call_graph_prefix_validation(self):
        for route in (("CLAUDE",), ("CLAUDE", "CODEX"), ("CLAUDE", "CODEX", "CLAUDE")):
            self.assertEqual(validate_call_graph_prefix(route), (True, None))
        self.assertEqual(validate_call_graph_prefix(("CODEX", "CLAUDE")), (False, "NEED_USER"))

class SupervisorIntegrationTests(unittest.TestCase):
    def setUp(self):
        # The integration suite verifies the Supervisor's state transitions and
        # atomic replace behavior.  A physical fsync is a filesystem durability
        # guarantee, not part of those assertions, and is disproportionately
        # slow on Windows temporary directories.
        fsync = patch("scripts.supervisor.runtime_store.os.fsync")
        fsync.start()
        self.addCleanup(fsync.stop)

    def _run(self, route, task_id="task-001"):
        directory = tempfile.TemporaryDirectory()
        supervisor = Supervisor(Path(directory.name) / "runtime", Path(directory.name) / "skills", _dry_run_adapters(route))
        return directory, supervisor, supervisor.run(task_id, route)

    # 17. Supervisor always derives LoopGuard from a route initial agent.
    def test_supervisor_sets_initial_agent(self):
        directory, _, state = self._run(DIRECT_CODEX)
        self.addCleanup(directory.cleanup)
        self.assertEqual(state["agent_history"], ["CODEX"])

    # 18-21. Every supported dry-run route reaches DONE.
    def test_dry_run_direct_codex(self):
        directory, _, state = self._run(DIRECT_CODEX)
        self.addCleanup(directory.cleanup)
        self.assertEqual(state["status"], "DONE")

    def test_dry_run_claude_only(self):
        directory, _, state = self._run(CLAUDE_ONLY)
        self.addCleanup(directory.cleanup)
        self.assertEqual(state["status"], "DONE")

    def test_dry_run_claude_codex(self):
        directory, _, state = self._run(CLAUDE_CODEX)
        self.addCleanup(directory.cleanup)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(state["agent_history"], ["CLAUDE", "CODEX"])

    def test_dry_run_task_records_route_step_total(self):
        directory, supervisor, _ = self._run(CLAUDE_CODEX)
        self.addCleanup(directory.cleanup)
        task = json.loads(supervisor.store.step_path("task-001", 1, "plan", "task").read_text(encoding="utf-8"))
        self.assertEqual((task["step_number"], task["step_total"]), (1, 2))

    def test_dry_run_claude_codex_review(self):
        directory, _, state = self._run(CLAUDE_CODEX_REVIEW)
        self.addCleanup(directory.cleanup)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(state["agent_history"], ["CLAUDE", "CODEX", "CLAUDE"])

    # 22. Dry-run presentation reports the expected event order.
    def test_dry_run_presentation_order(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = io.StringIO()
        supervisor = Supervisor(Path(directory.name) / "runtime", Path(directory.name) / "skills", _dry_run_adapters(CLAUDE_CODEX))
        with redirect_stdout(output):
            supervisor.run("task-001", CLAUDE_CODEX)
        text = output.getvalue()
        expected = ["[START] Step 1/2 CLAUDE", "[RUNNING] Step 1/2 CLAUDE", "[HANDOFF", "[HANDOFF RECEIVED", "[RUNNING] Step 2/2 CODEX", "[DONE] Task completed"]
        positions = [text.index(value) for value in expected]
        self.assertEqual(positions, sorted(positions))

    # 23. Handoff receipt identifies the source and target agents.
    def test_handoff_received_names_source_agent(self):
        from scripts.supervisor.presentation import emit_handoff_received
        output = io.StringIO()
        with redirect_stdout(output):
            emit_handoff_received("CLAUDE", "CODEX", "exec")
        self.assertIn("HANDOFF RECEIVED <- CLAUDE", output.getvalue())
        self.assertIn("CODEX -> exec", output.getvalue())

    # 24. Runtime state records DONE.
    def test_runtime_state_records_done(self):
        directory, supervisor, _ = self._run(CLAUDE_CODEX)
        self.addCleanup(directory.cleanup)
        self.assertEqual(supervisor.store.load_state("task-001")["status"], "DONE")

    # 25. Every dry-run step emits task and out machine JSON.
    def test_runtime_step_json_files_exist(self):
        directory, supervisor, _ = self._run(CLAUDE_CODEX)
        self.addCleanup(directory.cleanup)
        files = sorted(supervisor.store.task_root("task-001").glob("steps/*.json"))
        self.assertEqual(len(files), 4)
        self.assertTrue(any(path.name.endswith(".task.json") for path in files))
        self.assertTrue(any(path.name.endswith(".out.json") for path in files))

    # 26. Machine runtime JSON never includes terminal presentation or Persona text.
    def test_runtime_machine_json_is_presentation_free(self):
        directory, supervisor, _ = self._run(CLAUDE_CODEX)
        self.addCleanup(directory.cleanup)
        for path in supervisor.store.task_root("task-001").rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("I have Control!", text)
            self.assertNotIn("HANDOFF", text)
            self.assertNotIn("Task completed", text)

    # 27. Loop limits return a terminal status instead of continuing.
    def test_loop_limit_returns_terminal_status(self):
        guard = LoopGuard(current_agent="CLAUDE")
        guard.record_handoff("CODEX", "REPEAT")
        guard.record_handoff("CLAUDE", "REPEAT")
        allowed, status = guard.record_handoff("CODEX", "REPEAT")
        self.assertFalse(allowed)
        self.assertIn(status, {"NEED_USER", "FAILED"})
    # 28. Undefined call graphs become NEED_USER.
    def test_undefined_call_graph_returns_need_user(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Supervisor(Path(directory.name) / "runtime", Path(directory.name) / "skills", {}).run("task-001", ("CODEX", "CLAUDE"))
        self.assertEqual(state["status"], "NEED_USER")

    # 29. Atomic state writes leave readable JSON and no temporary artifact.
    def test_runtime_store_atomic_state_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(directory)
            store.write_state("task-001", {"status": "DONE"})
            self.assertEqual(store.load_state("task-001"), {"status": "DONE"})
            self.assertEqual(list(store.task_root("task-001").glob("*.tmp")), [])

    # 30. A completed task is never re-run.
    def test_done_task_is_not_re_run(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = _dry_run_adapters(DIRECT_CODEX)["CODEX"]
            supervisor = Supervisor(Path(directory) / "runtime", Path(directory) / "skills", {"CODEX": adapter})
            supervisor.run("task-001", DIRECT_CODEX)
            supervisor.run("task-001", DIRECT_CODEX)
            self.assertEqual(len(adapter.calls), 1)

class CapturedRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run_capture(self, task_id, command, *, input_text, cwd=None, env=None, timeout=None):
        self.calls.append((task_id, list(command), input_text, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CLIAdapterTests(unittest.TestCase):
    def setUp(self):
        discovery = patch("scripts.supervisor.cli_adapter.shutil.which", return_value="mock-cli")
        discovery.start()
        self.addCleanup(discovery.stop)

    def _task(self, step_id="01-plan"):
        return {"step_id": step_id, "role": "plan", "required_skills": [], "input_paths": [], "previous_step": "NONE", "return_to": "NONE"}

    # 31. Handoff receipt uses a received-direction arrow.
    def test_handoff_received_uses_received_arrow(self):
        from scripts.supervisor.presentation import emit_handoff_received
        output = io.StringIO()
        with redirect_stdout(output): emit_handoff_received("CLAUDE", "CODEX", "exec")
        self.assertIn("[HANDOFF RECEIVED <- CLAUDE] CODEX -> exec", output.getvalue())

    # 32-33. Adapters delegate process work to Runner only.
    def test_claude_adapter_uses_runner(self):
        runner = CapturedRunner([(0, '{"status":"OK","step_id":"01-plan"}\n', "")])
        self.assertEqual(ClaudeCLIAdapter(runner).execute(self._task())["status"], "OK")
        self.assertEqual(len(runner.calls), 1)

    def test_codex_adapter_uses_runner(self):
        runner = CapturedRunner([(0, '{"status":"OK","step_id":"02-exec"}\n', "")])
        self.assertEqual(CodexCLIAdapter(runner).execute(self._task("02-exec"))["status"], "OK")
        self.assertEqual(len(runner.calls), 1)

    def test_progress_description_includes_route_step_total(self):
        adapter = CodexCLIAdapter(CapturedRunner([]))
        task = self._task("02-exec") | {"agent": "CODEX", "step_number": 2, "step_total": 3}
        self.assertEqual(adapter._progress_description(task), "CODEX 02-exec (2/3단계) 진행 중")

    # 33b. Codex --json event-log output nests the real answer under item.text as a
    # JSON-encoded string; extraction must find it among the surrounding event lines.
    def test_codex_adapter_extracts_nested_item_text_from_json_event_log(self):
        event_log = "\n".join([
            '{"type":"thread.started","thread_id":"abc"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"{\\"status\\":\\"OK\\",\\"step_id\\":\\"02-exec\\",\\"handoff_request\\":null}"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
        ])
        runner = CapturedRunner([(0, event_log, "")])
        self.assertEqual(CodexCLIAdapter(runner).execute(self._task("02-exec"))["status"], "OK")

    # 34-35. Fake Claude and Codex CLI responses parse as strict machine payloads.
    def test_fake_claude_success(self):
        adapter = ClaudeCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"01-plan"}', "")]))
        self.assertEqual(adapter.execute(self._task())["step_id"], "01-plan")

    def test_fake_codex_success(self):
        adapter = CodexCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"02-exec"}', "")]))
        self.assertEqual(adapter.execute(self._task("02-exec"))["step_id"], "02-exec")

    # 36-38. Non-zero, timeout, and malformed outputs become adapter failures.
    def test_nonzero_exit_is_failure(self):
        with self.assertRaises(CLIAdapterError):
            ClaudeCLIAdapter(CapturedRunner([(2, "", "failure")])).execute(self._task())

    def test_timeout_is_failure(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            CodexCLIAdapter(CapturedRunner([subprocess.TimeoutExpired("codex", 1)])).execute(self._task())

    def test_malformed_machine_output_is_failure(self):
        with self.assertRaises(CLIAdapterError):
            ClaudeCLIAdapter(CapturedRunner([(0, "not json", "")])).execute(self._task())

    # 39. Unknown presentation fields are rejected at the adapter boundary.
    def test_adapter_output_stays_schema_valid(self):
        with self.assertRaises(CLIAdapterError):
            ClaudeCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"01-plan","presentation":"[DONE]"}', "")])).execute(self._task())

    # 40. Missing CLI is an explicit error.
    def test_cli_not_found(self):
        with patch("scripts.supervisor.cli_adapter.shutil.which", return_value=None):
            with self.assertRaises(CLIAdapterError): ClaudeCLIAdapter(CapturedRunner([])).execute(self._task())

    # 41. stdout and stderr are captured without being printed as raw logs.
    def test_stdout_stderr_capture(self):
        adapter = CodexCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"01-plan"}', "progress stderr")]))
        adapter.execute(self._task())
        self.assertEqual(adapter.last_stderr, "progress stderr")
        self.assertIn('"status"', adapter.last_stdout)

    # 42. Runner-backed adapters complete a mock CLAUDE -> CODEX chain.
    def test_adapter_mock_full_chain(self):
        claude = ClaudeCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"01-plan","handoff_request":{"target_agent":"CODEX","reason_code":"IMPLEMENTATION_REQUIRED","required_files":[],"required_skills":[],"return_to_agent":"NONE"}}', "")]))
        codex = CodexCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"02-exec"}', "")]))
        with tempfile.TemporaryDirectory() as directory:
            state = Supervisor(Path(directory) / "runtime", Path(directory) / "skills", {"CLAUDE": claude, "CODEX": codex}).run("task-001", CLAUDE_CODEX)
        self.assertEqual(state["status"], "DONE")

    # 43. Completed work prevents a second adapter invocation.
    def test_adapter_done_task_is_not_reinvoked(self):
        runner = CapturedRunner([(0, '{"status":"OK","step_id":"01-exec"}', "")])
        adapter = CodexCLIAdapter(runner)
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(Path(directory) / "runtime", Path(directory) / "skills", {"CODEX": adapter})
            supervisor.run("task-001", DIRECT_CODEX)
            supervisor.run("task-001", DIRECT_CODEX)
        self.assertEqual(len(runner.calls), 1)
    # 44. Test-injected paths avoid any dependency on installed CLIs.
    def test_injected_binary_skips_discovery(self):
        with patch("scripts.supervisor.cli_adapter.shutil.which") as discover:
            adapter = ClaudeCLIAdapter(CapturedRunner([(0, '{"status":"OK","step_id":"01-plan"}', "")]), executable_path="mock-claude")
            self.assertEqual(adapter.execute(self._task())["status"], "OK")
        discover.assert_not_called()

    # 45. Both CLI structured-output modes use schema.py as their single source.
    def test_adapters_share_out_json_schema_source(self):
        claude = ClaudeCLIAdapter(CapturedRunner([]), executable_path="mock-claude")
        claude._binary = "mock-claude"
        self.assertEqual(json.loads(claude._command(self._task())[-1]), structured_out_json_schema())
        codex = CodexCLIAdapter(CapturedRunner([]), executable_path="mock-codex")
        codex._binary = "mock-codex"
        command = codex._command(self._task())
        schema_path = Path(command[-1])
        try:
            self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")), structured_out_json_schema())
        finally:
            schema_path.unlink(missing_ok=True)

    # 46. Strict structured handoff output remains accepted end to end.
    def test_structured_handoff_output_passes_strict_schema(self):
        payload = '{"status":"OK","step_id":"01-plan","handoff_request":{"target_agent":"CODEX","reason_code":"IMPLEMENTATION_REQUIRED","required_files":[],"required_skills":[],"return_to_agent":"NONE"}}'
        output = ClaudeCLIAdapter(CapturedRunner([(0, payload, "")])).execute(self._task())
        validate_out_json(output, current_agent="CLAUDE")
    # 47. Provider-required null placeholders normalize back to the canonical contract.
    def test_structured_null_placeholders_normalize_to_canonical_output(self):
        payload = {"status": "OK", "step_id": "01-plan", "handoff_request": None}
        self.assertEqual(normalize_structured_out_json(payload), {"status": "OK", "step_id": "01-plan"})
if __name__ == "__main__":
    unittest.main()
