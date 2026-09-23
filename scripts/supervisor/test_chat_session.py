import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .chat_adapter import (
    ChatAdapterInterrupted,
    ClaudeChatAdapter,
    CodexChatAdapter,
    _read_codex_default_model,
    format_resolved_model,
)
from .chat_session import ChatSession, StreamPrinter, Turn, _addressed_agent, build_prompt, format_history_block, parse_input
from .stream_text import ChatFragment
from .timing import TurnTiming
from .usage import TokenUsage


class FakeAgent:
    def __init__(self, name: str, reply: str = "ok") -> None:
        self.display_name = name
        self.reply = reply
        self.calls: list[str] = []
        self.reset_calls = 0

    def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
        self.calls.append(prompt)
        return self.reply

    def reset_session(self) -> None:
        self.reset_calls += 1


class ParseInputTests(unittest.TestCase):
    def test_default_routing(self) -> None:
        self.assertEqual(parse_input("hello there", "Claude"), ("Claude", "hello there"))

    def test_mention_routes_to_target(self) -> None:
        self.assertEqual(parse_input("@codex fix the bug", "Claude"), ("Codex", "fix the bug"))
        self.assertEqual(parse_input("@에이드 fix the bug", "Claude"), ("Codex", "fix the bug"))
        self.assertEqual(parse_input("@블랑 review this", "Codex"), ("Claude", "review this"))
        self.assertEqual(parse_input("@both status check", "Codex"), ("Both", "status check"))

    def test_unrecognized_mention_falls_back_to_default(self) -> None:
        self.assertEqual(parse_input("@nobody hi", "Claude"), ("Claude", "@nobody hi"))

    def test_slash_command_has_no_target(self) -> None:
        self.assertEqual(parse_input("/exit", "Claude"), (None, "/exit"))

    def test_address_detection_requires_explicit_address_and_accepts_bold_korean(self) -> None:
        self.assertEqual(_addressed_agent("**Codex에게**: 확인 부탁해"), "Codex")
        self.assertEqual(_addressed_agent("클로드한테: 검토해줘"), "Claude")
        self.assertEqual(_addressed_agent("에이드에게: 구현 결과를 확인해줘"), "Codex")
        self.assertEqual(_addressed_agent("**블랑에게**: 요구사항을 검토해줘"), "Claude")
        self.assertIsNone(_addressed_agent("Codex가 아까 구현한 기능입니다."))


class HistoryFormattingTests(unittest.TestCase):
    def test_caps_by_char_budget(self) -> None:
        turns = [Turn(speaker="You", text="x" * 5000) for _ in range(5)]
        self.assertLessEqual(len(format_history_block(turns)), 12000)

    def test_keeps_at_least_one_line_even_if_over_budget(self) -> None:
        self.assertIn("You:", format_history_block([Turn(speaker="You", text="x" * 50000)]))


class BuildPromptTests(unittest.TestCase):
    def test_mentions_other_agent_and_new_message(self) -> None:
        prompt = build_prompt("Claude", "Codex", [], "hi")
        self.assertIn("You are Claude (S.O.P.H.I.A persona: 블랑)", prompt)
        self.assertIn("Codex (에이드", prompt)
        self.assertIn("You: hi", prompt)
        self.assertIn("personas/blanc_persona.txt", prompt)
        self.assertIn("every reply", prompt)

    def test_includes_condensed_earlier_history(self) -> None:
        prompt = build_prompt("Claude", "Codex", [], "hi", "You: old decision")
        self.assertIn("Earlier conversation, condensed", prompt)
        self.assertIn("old decision", prompt)


class ChatSessionTests(unittest.TestCase):
    def _session(self) -> tuple[ChatSession, FakeAgent, FakeAgent]:
        claude, codex = FakeAgent("Claude", "hi from claude"), FakeAgent("Codex", "hi from codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, default_agent="Claude",
                              printer=lambda speaker, text: None)
        return session, claude, codex

    def test_plain_message_goes_to_default_agent(self) -> None:
        session, claude, codex = self._session()
        session.handle_line("hello")
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 0)

    def test_run_command_dispatches_through_the_normal_task_path(self) -> None:
        session, claude, codex = self._session()
        self.assertTrue(session.handle_line("/run implement the change"))
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 0)
        self.assertIn("You: implement the change", claude.calls[0])

    def test_plain_message_goes_to_both_agents_when_both_is_default(self) -> None:
        claude, codex = FakeAgent("Claude"), FakeAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, default_agent="Both", printer=lambda *_: None)
        session.handle_line("hello")
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))

    def test_mention_routes_to_named_agent(self) -> None:
        session, claude, codex = self._session()
        session.handle_line("@codex do the thing")
        self.assertEqual(len(claude.calls), 0)
        self.assertEqual(len(codex.calls), 1)

    def test_both_mention_calls_each_agent_once(self) -> None:
        session, claude, codex = self._session()
        session.handle_line("@both sync up")
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 1)

    def test_both_does_not_start_automatic_relay(self) -> None:
        claude = FakeAgent("Claude", "Codex에게: 이 메시지는 relay하면 안 됩니다")
        codex = FakeAgent("Codex", "Claude에게: 이 메시지도 relay하면 안 됩니다")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda *_: None)
        session.handle_line("@both 의견을 주세요")
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))

    def test_both_announces_sequential_progress(self) -> None:
        output: list[tuple[str, str]] = []
        session, _, _ = self._session()
        session.printer = lambda speaker, text: output.append((speaker, text))
        session.handle_line("@both sync up")
        self.assertIn(("System", "@both: Claude → Codex 순서로 진행 중"), output)
        self.assertIn(("System", "Claude: 응답 완료"), output)
        self.assertIn(("System", "Codex: 응답 완료"), output)

    def test_both_calls_sequentially_and_second_prompt_includes_first_reply(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingAgent(FakeAgent):
            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.calls.append(prompt)
                started.set()
                if not release.wait(1.0):
                    raise RuntimeError("test release timed out")
                return self.reply

        claude, codex = BlockingAgent("Claude", "claude complete"), BlockingAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda *_: None)
        worker = threading.Thread(target=lambda: session.handle_line("@both sequential"))
        worker.start()
        self.assertTrue(started.wait(1.0))
        # Codex must not start until Claude's response is complete.
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 0))
        release.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))
        self.assertIn("Claude: claude complete", codex.calls[0])

    def test_delta_omits_already_seen_turns(self) -> None:
        session, claude, _ = self._session()
        session.handle_line("first")
        session.handle_line("second")
        self.assertIn("You: second", claude.calls[-1])
        self.assertNotIn("You: first", claude.calls[-1])
        self.assertNotIn("Claude: hi from claude", claude.calls[-1])

    def test_resume_reset_silently_rehydrates_full_context_before_display(self) -> None:
        class ResettingAgent(FakeAgent):
            def __init__(self) -> None:
                super().__init__("Claude", "full-context reply")
                self.last_reset = False

            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.calls.append(prompt)
                self.last_reset = len(self.calls) == 1
                return "incomplete reply" if self.last_reset else self.reply

        output: list[tuple[str, str]] = []
        claude, codex = ResettingAgent(), FakeAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session._record("You", "earlier context")
        session._last_sent_index["Claude"] = 1

        session.handle_line("current request")

        self.assertEqual(len(claude.calls), 2)
        self.assertNotIn("earlier context", claude.calls[0])
        self.assertIn("earlier context", claude.calls[1])
        self.assertIn("current request", claude.calls[1])
        self.assertEqual(session._last_sent_index["Claude"], len(session.history))
        self.assertIn(("Claude", "full-context reply"), output)
        self.assertNotIn(("Claude", "incomplete reply"), output)

    def test_default_command_switches_routing(self) -> None:
        session, claude, codex = self._session()
        session.handle_line("/default codex")
        session.handle_line("hello")
        self.assertEqual(len(claude.calls), 0)
        self.assertEqual(len(codex.calls), 1)

    def test_default_command_can_switch_to_both(self) -> None:
        session, claude, codex = self._session()
        session.handle_line("/default both")
        session.handle_line("hello")
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))

    def test_automatic_relay_for_explicit_address(self) -> None:
        claude = FakeAgent("Claude", "Codex에게: 이 부분을 구현해줘")
        codex = FakeAgent("Codex", "구현했습니다.")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda *_: None)
        session.handle_line("작업을 나눠줘")
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))
        self.assertIn("Claude: Codex에게:", codex.calls[0])

    def test_automatic_relay_for_persona_alias(self) -> None:
        claude = FakeAgent("Claude", "에이드에게: 이 변경을 구현하고 테스트해줘")
        codex = FakeAgent("Codex", "구현과 테스트를 마쳤습니다.")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda *_: None)
        session.handle_line("작업을 나눠줘")
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))
        self.assertIn("Claude: 에이드에게:", codex.calls[0])

    def test_automatic_relay_announces_hop_progress(self) -> None:
        output: list[tuple[str, str]] = []
        claude = FakeAgent("Claude", "Codex에게: 확인해주세요")
        codex = FakeAgent("Codex", "완료")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("작업 시작")
        self.assertIn(("System", "auto-relay 1/4: Claude -> Codex"), output)

    def test_automatic_relay_stops_without_address(self) -> None:
        claude = FakeAgent("Claude", "Codex에게: 확인해줘")
        codex = FakeAgent("Codex", "확인 완료")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda *_: None)
        session.handle_line("시작")
        self.assertEqual((len(claude.calls), len(codex.calls)), (1, 1))

    def test_automatic_relay_hop_limit_returns_control_to_user(self) -> None:
        class PingPongAgent(FakeAgent):
            def __init__(self, name: str, target: str) -> None:
                super().__init__(name)
                self.target = target

            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.calls.append(prompt)
                return f"{self.target}에게: 계속 진행해줘"

        output: list[tuple[str, str]] = []
        claude = PingPongAgent("Claude", "Codex")
        codex = PingPongAgent("Codex", "Claude")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("시작")
        self.assertEqual((len(claude.calls), len(codex.calls)), (3, 2))
        self.assertIn("hop limit reached", output[-1][1])

    def test_relay_command_toggles_automatic_relay(self) -> None:
        claude = FakeAgent("Claude", "Codex에게: 확인해줘")
        codex = FakeAgent("Codex", "done")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda *_: None)
        session.handle_line("/relay off")
        session.handle_line("첫 요청")
        self.assertEqual(len(codex.calls), 0)
        session.handle_line("/relay on")
        session.handle_line("둘째 요청")
        self.assertEqual(len(codex.calls), 1)

    def test_exit_command_ends_session(self) -> None:
        session, _, _ = self._session()
        self.assertFalse(session.handle_line("/exit"))

    def test_history_accumulates_across_turns(self) -> None:
        session, claude, _ = self._session()
        session.handle_line("first")
        session.handle_line("second")
        self.assertEqual([turn.speaker for turn in session.history], ["You", "Claude", "You", "Claude"])

    def test_old_turns_are_condensed_and_recent_turns_remain_verbatim(self) -> None:
        session, claude, _ = self._session()
        for index in range(20):
            session.handle_line(f"message {index}")
        self.assertTrue(session.history_summary)
        self.assertLessEqual(len(session.history), 32)
        self.assertIn("message 0", session.history_summary)
        self.assertIn("message 19", claude.calls[-1])

    def test_compaction_periodically_resets_remote_sessions_and_rehydrates_summary(self) -> None:
        session, claude, codex = self._session()
        for index in range(20):
            session.handle_line(f"message {index}")
        self.assertGreaterEqual((claude.reset_calls, codex.reset_calls), (1, 1))
        session.handle_line("after compaction")
        self.assertIn("Earlier conversation, condensed", claude.calls[-1])
        self.assertIn("message 0", claude.calls[-1])

    def test_clear_resets_history_and_agent_sessions(self) -> None:
        session, claude, codex = self._session()
        session.history_summary = "You: old decision"
        session.handle_line("/clear")
        self.assertEqual(session.history_summary, "")
        self.assertEqual((claude.reset_calls, codex.reset_calls), (1, 1))

    def test_help_explains_messages_are_accepted_while_working(self) -> None:
        output: list[tuple[str, str]] = []
        claude, codex = FakeAgent("Claude"), FakeAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("/help")
        self.assertIn("queued in order", output[-1][1])

    def test_agent_error_is_surfaced_not_raised(self) -> None:
        class BoomAgent:
            display_name = "Claude"
            def send(self, task_id: str, prompt: str) -> str:
                raise RuntimeError("boom")

        session = ChatSession({"Claude": BoomAgent(), "Codex": FakeAgent("Codex")}, default_agent="Claude")
        session.handle_line("hello")
        self.assertIn("boom", session.history[-1].text)

    def test_prints_latency_line_when_agent_reports_timing(self) -> None:
        class TimedAgent(FakeAgent):
            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.calls.append(prompt)
                self.last_timing = TurnTiming(label=self.display_name, start=0.0, first_output=1.0, end=3.0)
                return self.reply

        output: list[tuple[str, str]] = []
        claude, codex = TimedAgent("Claude", "hi"), TimedAgent("Codex", "hi")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("hello")
        self.assertIn(("System", "Claude 3.0s (첫 출력 1.0s)"), output)

    def test_timing_line_hidden_after_timing_off(self) -> None:
        class TimedAgent(FakeAgent):
            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.calls.append(prompt)
                self.last_timing = TurnTiming(label=self.display_name, start=0.0, first_output=1.0, end=3.0)
                return self.reply

        output: list[tuple[str, str]] = []
        claude, codex = TimedAgent("Claude", "hi"), TimedAgent("Codex", "hi")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("/timing off")
        session.handle_line("hello")
        self.assertFalse(any("첫 출력" in text for _, text in output))

    def test_usage_is_displayed_and_accumulated(self) -> None:
        class UsageAgent(FakeAgent):
            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.calls.append(prompt)
                self.last_usage = TokenUsage(100, 40, 20)
                return self.reply

        output: list[tuple[str, str]] = []
        claude, codex = UsageAgent("Claude", "hi"), FakeAgent("Codex", "hi")
        session = ChatSession({"Claude": claude, "Codex": codex}, default_agent="Claude",
                              printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("hello")
        session.handle_line("/usage")
        self.assertIn(("System", "Claude tokens — 입력 100 (캐시 40) · 출력 20"), output)
        self.assertIn("합계 입력 100 (캐시 40) · 출력 20", output[-1][1])

    def test_usage_command_can_hide_per_reply_lines(self) -> None:
        class UsageAgent(FakeAgent):
            def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
                self.last_usage = TokenUsage(1, 0, 2)
                return self.reply

        output: list[tuple[str, str]] = []
        session = ChatSession({"Claude": UsageAgent("Claude"), "Codex": FakeAgent("Codex")},
                              default_agent="Claude", printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("/usage off")
        session.handle_line("hello")
        self.assertFalse(any("tokens —" in text for _, text in output))

    def test_usage_command_does_not_represent_unavailable_usage_as_zero(self) -> None:
        output: list[tuple[str, str]] = []
        session, _, _ = self._session()
        session.printer = lambda speaker, text: output.append((speaker, text))
        session.handle_line("/usage")
        self.assertEqual(output[-1], ("System", "session token usage unavailable (provider did not return usage)"))

    def test_model_command_resets_target_and_rehydrates_full_context(self) -> None:
        class SwitchableAgent(FakeAgent):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.model: str | None = None

            def set_model(self, model: str | None) -> bool:
                if self.model == model:
                    return False
                self.model = model
                self.reset_calls += 1
                return True

        output: list[tuple[str, str]] = []
        claude, codex = SwitchableAgent("Claude"), SwitchableAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, default_agent="Claude",
                              printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("earlier context")
        session.handle_line("/model claude custom-model")
        session.handle_line("current request")
        self.assertEqual((claude.model, claude.reset_calls), ("custom-model", 1))
        self.assertIn("earlier context", claude.calls[-1])
        self.assertIn("current request", claude.calls[-1])
        self.assertTrue(any("full context will be restored" in text for _, text in output))

    def test_model_command_shows_models_and_changes_both(self) -> None:
        class SwitchableAgent(FakeAgent):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.model: str | None = None

            def set_model(self, model: str | None) -> bool:
                if self.model == model:
                    return False
                self.model = model
                return True

        output: list[tuple[str, str]] = []
        claude, codex = SwitchableAgent("Claude"), SwitchableAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("/model")
        self.assertEqual(output[-1], ("System", "models — Claude default (unresolved); Codex default (unresolved)"))
        session.handle_line("/model both fast-model")
        self.assertEqual((claude.model, codex.model), ("fast-model", "fast-model"))

    def test_model_command_updates_a_model_aware_presenter(self) -> None:
        class SwitchableAgent(FakeAgent):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.model: str | None = None

            def set_model(self, model: str | None) -> bool:
                if self.model == model:
                    return False
                self.model = model
                return True

        class ModelAwarePrinter:
            def __init__(self) -> None:
                self.models: dict[str, str | None] = {}

            def __call__(self, speaker: str, text: str) -> None:
                pass

            def set_agent_model(self, speaker: str, model: str | None) -> None:
                self.models[speaker] = model

        printer = ModelAwarePrinter()
        session = ChatSession({"Claude": SwitchableAgent("Claude"), "Codex": SwitchableAgent("Codex")},
                              printer=printer)
        session.handle_line("/model codex gpt-5.6")
        self.assertEqual(printer.models, {"Codex": "gpt-5.6"})

    def test_attachment_is_sent_once_with_next_message(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachment = root / "reference image.png"
            attachment.write_bytes(b"test")
            output: list[tuple[str, str]] = []
            claude, codex = FakeAgent("Claude"), FakeAgent("Codex")
            session = ChatSession({"Claude": claude, "Codex": codex}, default_agent="Claude",
                                  printer=lambda speaker, text: output.append((speaker, text)), attachment_root=root)
            session.handle_line('/attach "reference image.png"')
            session.handle_line("/attachments")
            session.handle_line("inspect this")
            session.handle_line("next message")
        self.assertIn(("System", "attachment queued: reference image.png"), output)
        self.assertIn("Attached workspace files for this message", claude.calls[-2])
        self.assertIn(str(attachment), claude.calls[-2])
        self.assertNotIn("Attached workspace files", claude.calls[-1])

    def test_attachment_rejects_paths_outside_workspace_and_clear_discards_queue(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            attachment = root / "note.txt"
            attachment.write_text("note", encoding="utf-8")
            outside = Path(tmp) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            output: list[tuple[str, str]] = []
            session = ChatSession({"Claude": FakeAgent("Claude"), "Codex": FakeAgent("Codex")},
                                  printer=lambda speaker, text: output.append((speaker, text)), attachment_root=root)
            session.handle_line(f"/attach {outside}")
            session.handle_line("/attach note.txt")
            session.handle_line("/attach clear")
            session.handle_line("/attachments")
        self.assertIn(("System", "attachment rejected: path must stay inside the chat workspace"), output)
        self.assertIn(("System", "discarded 1 pending attachment"), output)
        self.assertIn(("System", "no pending attachments"), output)

    def test_steer_records_instruction_resets_context_and_cancels_active_agents(self) -> None:
        class SteerableAgent(FakeAgent):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.cancel_calls = 0

            def cancel_current(self) -> bool:
                self.cancel_calls += 1
                return True

        output: list[tuple[str, str]] = []
        claude, codex = SteerableAgent("Claude"), SteerableAgent("Codex")
        session = ChatSession({"Claude": claude, "Codex": codex},
                              printer=lambda speaker, text: output.append((speaker, text)))
        session._record("You", "original task")
        session._last_sent_index = {"Claude": 1, "Codex": 1}
        self.assertTrue(session.request_steer("prioritize the failing test"))
        self.assertEqual((claude.cancel_calls, codex.cancel_calls), (1, 1))
        self.assertEqual(session._last_sent_index, {"Claude": 0, "Codex": 0})
        self.assertIn("Additional instruction", session.history[-1].text)
        self.assertIn("prioritize the failing test", session.history[-1].text)
        self.assertIn(("System", "steering accepted: current work will restart with the additional instruction"), output)

    def test_no_timing_line_when_agent_lacks_timing_info(self) -> None:
        output: list[tuple[str, str]] = []
        claude, codex = FakeAgent("Claude", "hi"), FakeAgent("Codex", "hi")
        session = ChatSession({"Claude": claude, "Codex": codex}, printer=lambda speaker, text: output.append((speaker, text)))
        session.handle_line("hello")
        self.assertFalse(any("s (첫 출력" in text or text.endswith("s") for _, text in output))

    def test_log_file_written(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            claude, codex = FakeAgent("Claude", "hi"), FakeAgent("Codex", "hi")
            session = ChatSession({"Claude": claude, "Codex": codex}, default_agent="Claude", log_path=log_path)
            session.handle_line("hello")
            self.assertIn("You: hello", log_path.read_text(encoding="utf-8"))

    def test_log_indents_continuation_lines_for_multiline_turn(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            session = ChatSession(
                {"Claude": FakeAgent("Claude"), "Codex": FakeAgent("Codex")},
                log_path=log_path,
            )
            session._record("Codex", "first line\nsecond line\nthird line")

            lines = log_path.read_text(encoding="utf-8").splitlines()
            prefix_length = len(lines[0]) - len("first line")
            self.assertRegex(lines[0], r"^\[\d{2}:\d{2}:\d{2}\] Codex: first line$")
            self.assertEqual(lines[1], " " * prefix_length + "second line")
            self.assertEqual(lines[2], " " * prefix_length + "third line")


class StreamPrinterTests(unittest.TestCase):
    def test_codex_completed_block_uses_word_pacing_when_enabled(self) -> None:
        printer = StreamPrinter(
            lambda _line: ChatFragment("one two three", is_block=True),
            typewriter_speakers={"Codex"},
            typing_delay=0.02,
        )
        with patch("builtins.print") as printed, patch("scripts.supervisor.chat_session.time.sleep") as sleep:
            printer.on_line("Codex", "completed")

        self.assertEqual([call.args[0] for call in printed.call_args_list], ["Codex: ", "one ", "two ", "three"])
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.02)

    def test_completed_blocks_remain_immediate_when_pacing_is_disabled(self) -> None:
        printer = StreamPrinter(lambda _line: ChatFragment("complete reply", is_block=True))
        with patch("builtins.print") as printed, patch("scripts.supervisor.chat_session.time.sleep") as sleep:
            printer.on_line("Codex", "completed")

        self.assertEqual([call.args[0] for call in printed.call_args_list], ["Codex: ", "complete reply"])
        sleep.assert_not_called()

    def test_parallel_stream_callback_waits_for_current_render(self) -> None:
        printer = StreamPrinter(lambda line: ChatFragment(line, is_block=False))
        started = threading.Event()

        def render_from_codex() -> None:
            started.set()
            printer.on_line("Codex", "reply")

        with patch("builtins.print") as printed:
            with printer._lock:
                worker = threading.Thread(target=render_from_codex)
                worker.start()
                self.assertTrue(started.wait(1.0))
                worker.join(0.05)
                self.assertTrue(worker.is_alive())
                self.assertFalse(printed.called)
            worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual([call.args[0] for call in printed.call_args_list], ["Codex: ", "reply"])


class CapturedRunner:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def run_capture(self, task_id, command, *, input_text, cwd=None, timeout=None):
        self.calls.append(list(command))
        return self.responses.pop(0)


class ChatAdapterSessionTests(unittest.TestCase):
    def test_codex_default_model_reads_codex_home_config_and_marks_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.toml").write_text('model = "configured-model"\n', encoding="utf-8")
            with patch.dict("os.environ", {"CODEX_HOME": tmp}, clear=False):
                adapter = CodexChatAdapter(CapturedRunner([]), cwd="C:/work", executable_path="codex", show_progress=False)
                self.assertEqual(_read_codex_default_model(), "configured-model")
        self.assertEqual((adapter.resolved_model, adapter.resolved_model_source), ("configured-model", "configured"))
        self.assertEqual(format_resolved_model(adapter), "configured-model (configured)")

    def test_codex_default_model_ignores_invalid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.toml").write_text("model = [\n", encoding="utf-8")
            with patch.dict("os.environ", {"CODEX_HOME": tmp}, clear=False):
                adapter = CodexChatAdapter(CapturedRunner([]), cwd="C:/work", executable_path="codex", show_progress=False)
                self.assertIsNone(_read_codex_default_model())
        self.assertEqual((adapter.resolved_model, adapter.resolved_model_source), (None, None))

    def test_claude_reported_model_is_not_tagged_as_configured(self) -> None:
        adapter = ClaudeChatAdapter(CapturedRunner([]), cwd="C:/work", executable_path="claude", show_progress=False)
        adapter._handle_event_line('{"type":"system","subtype":"init","model":"reported-model"}')
        self.assertEqual((adapter.resolved_model, adapter.resolved_model_source), ("reported-model", "reported"))
        self.assertEqual(format_resolved_model(adapter), "reported-model")

    def test_relay_context_is_included_in_wait_progress(self) -> None:
        class CapturedProgress:
            descriptions: list[str] = []

            def __init__(self, desc: str, *, enabled: bool = True) -> None:
                self.descriptions.append(desc)

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        runner = CapturedRunner([(0, '{"type":"result","result":"done","session_id":"session-1"}', "")])
        adapter = ClaudeChatAdapter(runner, cwd="C:/work", executable_path="claude", show_progress=True)
        with patch(f"{ClaudeChatAdapter.__module__}.WaitProgress", CapturedProgress):
            self.assertEqual(adapter.send("a", "one", context="relay 1/4"), "done")
        self.assertEqual(CapturedProgress.descriptions, ["Claude 응답 대기 중 (relay 1/4)"])

    def test_claude_resumes_captured_session(self) -> None:
        runner = CapturedRunner([
            (0, '{"type":"result","result":"first","session_id":"session-1"}', ""),
            (0, '{"type":"result","result":"second","session_id":"session-1"}', ""),
        ])
        adapter = ClaudeChatAdapter(runner, cwd="C:/work", executable_path="claude", show_progress=False)
        self.assertEqual(adapter.send("a", "one"), "first")
        self.assertEqual(adapter.send("a", "two"), "second")
        self.assertIn("--resume", runner.calls[1])
        self.assertIn("session-1", runner.calls[1])

    def test_claude_model_is_added_to_each_command(self) -> None:
        runner = CapturedRunner([(0, '{"type":"result","result":"done","session_id":"session-1"}', "")])
        adapter = ClaudeChatAdapter(runner, cwd="C:/work", executable_path="claude", show_progress=False, model="claude-test")
        adapter.send("a", "one")
        self.assertIn("--model", runner.calls[0])
        self.assertIn("claude-test", runner.calls[0])

    def test_model_change_resets_codex_session(self) -> None:
        adapter = CodexChatAdapter(CapturedRunner([]), cwd="C:/work", executable_path="codex", show_progress=False)
        adapter._session_id = "old-thread"
        self.assertTrue(adapter.set_model("new-model"))
        self.assertEqual((adapter.model, adapter._session_id), ("new-model", None))
        self.assertFalse(adapter.set_model("new-model"))

    def test_cancelled_chat_turn_is_reported_as_interrupted(self) -> None:
        class InterruptingRunner:
            def __init__(self) -> None:
                self.adapter = None
                self.cancelled = []

            def run_capture(self, task_id, command, *, input_text, cwd=None, timeout=None):
                self.adapter.cancel_current()
                return 1, "", "terminated"

            def cancel(self, task_id):
                self.cancelled.append(task_id)
                return True

        runner = InterruptingRunner()
        adapter = CodexChatAdapter(runner, cwd="C:/work", executable_path="codex", show_progress=False)
        runner.adapter = adapter
        with self.assertRaises(ChatAdapterInterrupted):
            adapter.send("chat-codex", "do work")
        self.assertEqual(runner.cancelled, ["chat-codex"])

    def test_codex_resumes_captured_thread(self) -> None:
        runner = CapturedRunner([
            (0, '{"type":"thread.started","thread_id":"thread-1"}\n{"type":"item.completed","item":{"type":"agent_message","text":"first"}}', ""),
            (0, '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}', ""),
        ])
        adapter = CodexChatAdapter(runner, cwd="C:/work", executable_path="codex", show_progress=False)
        adapter.send("a", "one")
        self.assertEqual(adapter.send("a", "two"), "second")
        self.assertEqual(runner.calls[1][:4], ["codex", "exec", "resume", "thread-1"])
        self.assertIn('sandbox_mode="workspace-write"', runner.calls[1])
        self.assertNotIn("--cd", runner.calls[1])

    def test_resume_failure_retries_once_as_fresh_session(self) -> None:
        runner = CapturedRunner([
            (0, '{"type":"thread.started","thread_id":"old"}\n{"type":"item.completed","item":{"type":"agent_message","text":"first"}}', ""),
            (1, "", "thread old not found for resume"),
            (0, '{"type":"thread.started","thread_id":"new"}\n{"type":"item.completed","item":{"type":"agent_message","text":"recovered"}}', ""),
        ])
        adapter = CodexChatAdapter(runner, cwd="C:/work", executable_path="codex", show_progress=False)
        adapter.send("a", "one")
        self.assertEqual(adapter.send("a", "two"), "recovered")
        self.assertIn("resume", runner.calls[1])
        self.assertNotIn("resume", runner.calls[2])
        self.assertEqual(adapter._session_id, "new")
        self.assertTrue(adapter.last_reset)


if __name__ == "__main__":
    unittest.main()
