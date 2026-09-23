"""Runner-backed Claude CLI adapter."""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .cli_adapter import CLIAdapter
from .schema import structured_out_json_schema

class ClaudeCLIAdapter(CLIAdapter):
    executable = "claude"

    def _command(self, task: Mapping[str, Any]) -> Sequence[str]:
        schema = structured_out_json_schema()
        return [self._binary, "--print", "--tools", "", "--output-format", "json", "--json-schema", json.dumps(schema)]

    def _streaming_command(self, task: Mapping[str, Any]) -> Sequence[str]:
        schema = structured_out_json_schema()
        return [self._binary, "--print", "--tools", "", "--verbose", "--output-format", "stream-json",
                "--include-partial-messages", "--json-schema", json.dumps(schema)]