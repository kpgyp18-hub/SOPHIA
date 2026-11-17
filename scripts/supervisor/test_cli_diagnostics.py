"""Regression coverage for failure messages hidden by CLI JSON/banners."""
from __future__ import annotations

import json
import unittest

from .chat_adapter import ChatAdapterError, ClaudeChatAdapter, CodexChatAdapter
from .claude_adapter import ClaudeCLIAdapter
from .cli_adapter import CLIAdapterError
from .stream_text import extract_cli_diagnostic


LIMIT_MESSAGE = "You've hit your 5-hour limit"


def claude_failure() -> str:
    # Match the observed result shape, including an apparently successful subtype
    # and enough metadata to push the actual cause outside the old tail slice.
    return json.dumps({"type": "result", "subtype": "success", "is_error": True,
                       "api_error": "usage_limit_reached", "api_error_status": 429,
                       "result": LIMIT_MESSAGE, "usage": {"padding": "x" * 1200},
                       "credential_expires_at": "PRIVATE_METADATA"})


class FailureRunner:
    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout, self.stderr = stdout, stderr
        self.calls = 0

    def run_capture(self, *args, **kwargs):
        self.calls += 1
        return 1, self.stdout, self.stderr

    def run_capture_streaming(self, *args, on_line, **kwargs):
        for line in self.stdout.splitlines():
            on_line(line)
        return self.run_capture(*args, **kwargs)


class CLIDiagnosticTests(unittest.TestCase):
    def test_claude_limit_survives_large_trailing_metadata(self):
        diagnostic = extract_cli_diagnostic(claude_failure(), "")
        self.assertEqual(diagnostic, "usage_limit_reached: " + LIMIT_MESSAGE)
        self.assertNotIn("PRIVATE_METADATA", diagnostic)

    def test_codex_turn_failure_overrides_stdin_banner(self):
        stdout = json.dumps({"type": "turn.failed", "error": {"message": "session has an active writer"}})
        self.assertEqual(extract_cli_diagnostic(stdout, "Reading prompt from stdin..."),
                         "session has an active writer")

    def test_terminal_error_wins_over_intermediate_error(self):
        stdout = '\n'.join(json.dumps(event) for event in [
            {"type": "error", "message": "Reconnecting..."},
            {"type": "turn.failed", "error": {"code": "quota", "message": "usage exhausted"}},
            {"type": "system", "message": "PRIVATE_METADATA"},
        ])
        self.assertEqual(extract_cli_diagnostic(stdout, ""), "quota: usage exhausted")

    def test_string_errors_and_claude_error_arrays(self):
        self.assertEqual(extract_cli_diagnostic('{"type":"error","error":"connection failed"}', ""),
                         "connection failed")
        stdout = json.dumps({"type": "result", "subtype": "error_during_execution",
                             "errors": ["failure", {"message": "details"}, None, 1]})
        self.assertEqual(extract_cli_diagnostic(stdout, ""), "failure: details")

    def test_success_and_metadata_are_not_error_diagnostics(self):
        stdout = json.dumps({"type": "result", "result": "PRIVATE_REPLY", "is_error": False})
        self.assertEqual(extract_cli_diagnostic(stdout, "permission denied"), "permission denied")
        self.assertEqual(extract_cli_diagnostic(stdout, ""), "no error diagnostic")

    def test_plain_text_fallback_and_empty_output(self):
        self.assertEqual(extract_cli_diagnostic("stdout failure", "stderr failure"), "stderr failure")
        self.assertEqual(extract_cli_diagnostic("stdout failure", ""), "stdout failure")
        self.assertEqual(extract_cli_diagnostic("", ""), "no error diagnostic")
        self.assertEqual(extract_cli_diagnostic('null\n[]\n42\n{"type":"error","error":null}', ""),
                         "no error diagnostic")

    def test_structured_error_is_bounded_from_the_beginning(self):
        stdout = json.dumps({"type": "error", "message": "quota reached " + "x" * 1000})
        diagnostic = extract_cli_diagnostic(stdout, "", limit=240)
        self.assertEqual(len(diagnostic), 240)
        self.assertTrue(diagnostic.startswith("quota reached"))

    def test_chat_both_providers_keep_failure_outcome_and_make_one_call(self):
        codex_failure = json.dumps({"type": "turn.failed", "error": {"message": "quota reached"}})
        for adapter_type, stdout, stderr, expected in [
            (ClaudeChatAdapter, claude_failure(), "", LIMIT_MESSAGE),
            (CodexChatAdapter, codex_failure, "Reading prompt from stdin...", "quota reached"),
        ]:
            for streaming in (False, True):
                with self.subTest(adapter=adapter_type.__name__, streaming=streaming):
                    runner = FailureRunner(stdout, stderr)
                    adapter = adapter_type(runner, cwd=".", executable_path="fake-cli",
                                           show_progress=False,
                                           on_line=(lambda line: None) if streaming else None)
                    with self.assertRaisesRegex(ChatAdapterError, expected):
                        adapter.send("diagnostic-test", "hi")
                    self.assertEqual(runner.calls, 1)
                    self.assertEqual(adapter.last_timing.outcome, "nonzero_exit")
                    self.assertEqual(adapter.last_timing.exit_code, 1)

    def test_supervisor_adapter_uses_same_diagnostic(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                runner = FailureRunner(claude_failure())
                adapter = ClaudeCLIAdapter(runner, executable_path="fake-cli", show_progress=False,
                                           on_line=(lambda line: None) if streaming else None)
                with self.assertRaisesRegex(CLIAdapterError, LIMIT_MESSAGE):
                    adapter.execute({"step_id": "diagnostic-test"})
                self.assertEqual(runner.calls, 1)
                self.assertEqual(adapter.last_timing.outcome, "nonzero_exit")
