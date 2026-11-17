import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from .usage import TokenUsage, UsageLog, UsageTracker, extract_token_usage
from .chat_adapter import ChatAdapterError, CodexChatAdapter
from .chat_session import ChatSession


class TokenUsageTests(unittest.TestCase):
    def test_extracts_codex_turn_completed_usage(self) -> None:
        raw = '\n'.join((
            '{"type":"turn.started"}',
            '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":30,"reasoning_output_tokens":10}}',
        ))
        self.assertEqual(extract_token_usage(raw), TokenUsage(120, 80, 30, 10))

    def test_extracts_claude_result_usage(self) -> None:
        raw = '{"type":"result","usage":{"input_tokens":120,"cache_read_input_tokens":80,"output_tokens":30}}'
        self.assertEqual(extract_token_usage(raw), TokenUsage(120, 80, 30))

    def test_uses_final_usage_event(self) -> None:
        raw = '\n'.join((
            '{"usage":{"input_tokens":1,"output_tokens":2}}',
            '{"usage":{"input_tokens":3,"output_tokens":4}}',
        ))
        self.assertEqual(extract_token_usage(raw), TokenUsage(3, 0, 4))

    def test_log_writes_only_available_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            log = UsageLog(path)
            log.record("Codex", None)
            log.record("Codex", TokenUsage(1, 0, 2))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows, [{"label": "Codex", "input_tokens": 1, "cached_input_tokens": 0,
                                 "output_tokens": 2, "reasoning_output_tokens": 0}])


class UsageTrackerTests(unittest.TestCase):
    def observe(self, tracker, value, *, new=False, session="s"):
        return tracker.observe(value, scope="session", session_id=session, new_session=new)

    def test_session_totals_are_differenced_and_reset_starts_a_new_baseline(self):
        tracker = UsageTracker()
        first = self.observe(tracker, TokenUsage(100, 40, 20), new=True)
        second = self.observe(tracker, TokenUsage(250, 90, 50))
        fresh = self.observe(tracker, TokenUsage(80, 20, 10), new=True, session="fresh")
        self.assertEqual((first.incremental, second.incremental, fresh.incremental),
                         (TokenUsage(100, 40, 20), TokenUsage(150, 50, 30), TokenUsage(80, 20, 10)))

    def test_missing_report_preserves_baseline_and_marks_interval_attribution(self):
        tracker = UsageTracker()
        self.observe(tracker, TokenUsage(100), new=True)
        self.assertIsNone(self.observe(tracker, None))
        next_report = self.observe(tracker, TokenUsage(400))
        self.assertEqual(next_report.incremental.input_tokens, 300)
        self.assertEqual(next_report.attribution, "since_last_report")

    def test_interrupted_new_session_has_a_known_zero_baseline(self):
        tracker = UsageTracker()
        self.observe(tracker, None, new=True)
        next_report = self.observe(tracker, TokenUsage(400))
        self.assertEqual(next_report.incremental.input_tokens, 400)
        self.assertEqual(next_report.attribution, "since_last_report")

    def test_unfamiliar_resume_and_decreasing_counters_are_not_invented_usage(self):
        tracker = UsageTracker()
        first = self.observe(tracker, TokenUsage(500))
        self.assertIsNone(first.incremental)
        self.assertEqual(first.attribution, "baseline_only")
        reset = self.observe(tracker, TokenUsage(20))
        self.assertIsNone(reset.incremental)
        self.assertEqual(reset.attribution, "counter_reset")
        self.assertEqual(self.observe(tracker, TokenUsage(30)).incremental.input_tokens, 10)

    def test_missing_identity_or_unknown_scope_only_retains_raw_values(self):
        tracker = UsageTracker()
        self.assertIsNone(self.observe(tracker, TokenUsage(99), new=True, session=None).incremental)
        observation = tracker.observe(TokenUsage(99), scope="unknown", session_id="s", new_session=True)
        self.assertIsNone(observation.incremental)
        self.assertEqual(observation.raw.input_tokens, 99)

    def test_explicit_new_generation_does_not_reuse_even_the_same_identity(self):
        tracker = UsageTracker()
        self.observe(tracker, TokenUsage(100), new=True)
        self.assertEqual(self.observe(tracker, TokenUsage(20), new=True).incremental.input_tokens, 20)

    def test_raw_only_log_does_not_publish_a_misleading_incremental_zero(self):
        tracker = UsageTracker()
        observation = self.observe(tracker, TokenUsage(99))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            UsageLog(path).record("Codex", observation.incremental, observation=observation)
            row = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(row["incremental_available"])
        self.assertNotIn("input_tokens", row)
        self.assertEqual(row["raw_usage"]["input_tokens"], 99)

    def test_turn_scope_is_not_differenced(self):
        tracker = UsageTracker()
        for _ in range(2):
            self.assertEqual(tracker.observe(TokenUsage(50), scope="turn", session_id="s",
                                            new_session=False).incremental.input_tokens, 50)


class AdapterUsageTests(unittest.TestCase):
    def test_saved_cumulative_sample_is_counted_once_despite_an_interrupted_turn(self):
        def output(session_id, count):
            return '\n'.join((json.dumps({"type": "thread.started", "thread_id": session_id}),
                              json.dumps({"type": "turn.completed", "usage": {"input_tokens": count}})))
        class Runner:
            responses = [output("session-a", 16341),
                         subprocess.TimeoutExpired("local-double", 1, output='{"type":"thread.started","thread_id":"session-a"}'),
                         output("session-a", 943385), output("session-b", 16341)]
            def run_capture(self, *args, **kwargs):
                response = self.responses.pop(0)
                if isinstance(response, BaseException):
                    raise response
                return 0, response, ""
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CodexChatAdapter(Runner(), cwd=tmp, executable_path="local-double", show_progress=False,
                                       usage_log=UsageLog(Path(tmp) / "usage.jsonl"))
            total = TokenUsage()
            adapter.send("chat-codex", "short")
            total += adapter.last_usage
            with self.assertRaises(ChatAdapterError):
                adapter.send("chat-codex", "long")
            adapter.send("chat-codex", "new short request")
            self.assertEqual(adapter.last_usage_observation.attribution, "since_last_report")
            total += adapter.last_usage
            adapter.reset_session()
            adapter.send("chat-codex", "fresh")
            total += adapter.last_usage
            self.assertEqual(total.input_tokens, 959726)
            rows = [json.loads(line) for line in (Path(tmp) / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[1]["raw_usage"]["input_tokens"], 943385)
            self.assertEqual(rows[1]["input_tokens"], 927044)

    def test_consumption_never_substitutes_for_a_context_measurement(self):
        class Agent:
            last_usage = TokenUsage(input_tokens=943385)
            last_context_input_tokens = None
            resets = 0
            def reset_session(self): self.resets += 1
        agent = Agent()
        session = ChatSession({"Claude": Agent(), "Codex": agent}, printer=lambda *_: None)
        session._last_sent_index["Codex"] = 1
        session._reset_heavy_remote_session("Codex")
        self.assertEqual(agent.resets, 0)
        agent.last_context_input_tokens = 150000
        session._reset_heavy_remote_session("Codex")
        self.assertEqual(agent.resets, 1)

    def test_ui_counts_hidden_attempts_once_and_does_not_repeat_stale_usage(self):
        class Agent:
            usage_total = TokenUsage(300, 100, 20)
            last_usage = TokenUsage(50, 10, 2)  # Only the final hidden attempt.
        agent = Agent()
        session = ChatSession({"Claude": Agent(), "Codex": agent}, printer=lambda *_: None)
        session._print_usage("Codex", agent)
        self.assertEqual(session.usage_totals["Codex"], TokenUsage(300, 100, 20))
        session._print_usage("Codex", agent)
        self.assertEqual(session.usage_totals["Codex"], TokenUsage(300, 100, 20))
        agent.usage_total = TokenUsage(340, 120, 24)
        session._print_usage("Codex", agent)
        self.assertEqual(session.usage_totals["Codex"], TokenUsage(340, 120, 24))

