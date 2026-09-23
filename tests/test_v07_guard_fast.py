from sophia.safety.classifier import SafetyVerdict
from sophia.safety.guard_fast import LlamaGuardAdapter


def test_guard_parses_strict_safe_and_unsafe_protocol():
    assert LlamaGuardAdapter.parse("safe\n", model="guard").verdict is SafetyVerdict.SAFE
    result = LlamaGuardAdapter.parse("unsafe\nS1, S11\n", model="guard")
    assert result.verdict is SafetyVerdict.UNSAFE
    assert result.categories == ("S1", "S11")


def test_guard_fails_closed_for_malformed_or_empty_results():
    assert LlamaGuardAdapter.parse("maybe safe").verdict is SafetyVerdict.UNKNOWN
    assert LlamaGuardAdapter.parse("").requires_escalation


def test_guard_uses_ollama_chat_template_and_keeps_no_model_resident():
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"message": {"content": "safe"}}'

    def opener(request, timeout):
        captured["url"], captured["timeout"] = request.full_url, timeout
        captured["body"] = request.data.decode("utf-8")
        return Response()

    result = LlamaGuardAdapter(opener=opener).classify("Summarize this text.")
    assert result.verdict is SafetyVerdict.SAFE
    assert captured["url"].endswith("/api/chat")
    assert '"keep_alive": 0' in captured["body"]
