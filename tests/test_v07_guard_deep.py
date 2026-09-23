from sophia.safety.classifier import SafetyVerdict
from sophia.safety.guard_deep import GraniteGuardianAdapter


def test_guardian_parses_harm_score_contract():
    assert GraniteGuardianAdapter.parse("<think></think><score>no</score>").verdict is SafetyVerdict.SAFE
    assert GraniteGuardianAdapter.parse("<score> yes </score>").verdict is SafetyVerdict.UNSAFE


def test_guardian_fails_closed_for_unknown_or_non_safety_judgement():
    assert GraniteGuardianAdapter.parse("no").verdict is SafetyVerdict.UNKNOWN
    result = GraniteGuardianAdapter.parse("<score>no</score>", criterion="groundedness")
    assert result.verdict is SafetyVerdict.UNKNOWN
    assert "Judge Layer" in result.detail


def test_guardian_uses_its_criteria_template_and_cold_loads():
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"message": {"content": "<think></think><score>no</score>"}}'

    def opener(request, timeout):
        captured["url"], captured["timeout"] = request.full_url, timeout
        captured["body"] = request.data.decode("utf-8")
        return Response()

    result = GraniteGuardianAdapter(opener=opener).classify("A harmless request.")
    assert result.verdict is SafetyVerdict.SAFE
    assert captured["url"].endswith("/api/chat")
    assert '"content": "harm"' in captured["body"]
    assert '"keep_alive": 0' in captured["body"]
