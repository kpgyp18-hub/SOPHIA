import json
import tempfile
import unittest
from pathlib import Path

from .usage import TokenUsage, UsageLog, extract_token_usage


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

