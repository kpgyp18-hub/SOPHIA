import unittest
from unittest.mock import patch

from .chat_presentation import ChatPresenter


class ChatPresenterTests(unittest.TestCase):
    def test_model_label_is_added_to_agent_title(self) -> None:
        presenter = ChatPresenter(enabled=False)
        presenter.set_agent_models({"Codex": "gpt-5.6"})
        self.assertEqual(presenter._title("Codex"), ("ADE · CODEX · gpt-5.6", "bold yellow"))

    def test_default_model_label_does_not_claim_a_concrete_model(self) -> None:
        presenter = ChatPresenter(enabled=False)
        presenter.set_agent_models({"Claude": None})
        self.assertEqual(presenter._title("Claude"), ("BLANC · CLAUDE · provider default", "bold cyan"))

    def test_plain_fallback_preserves_existing_line_format(self) -> None:
        presenter = ChatPresenter(enabled=False)
        with patch("builtins.print") as printed:
            presenter("Claude", "hello")
        printed.assert_called_once_with("Claude: hello")

    def test_plain_stream_prefix_preserves_existing_format(self) -> None:
        presenter = ChatPresenter(enabled=False)
        with patch("builtins.print") as printed:
            presenter.stream_prefix("Codex")
        printed.assert_called_once_with("Codex: ", end="", flush=True)
