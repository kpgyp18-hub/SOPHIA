"""Runner-backed Codex CLI adapter."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cli_adapter import CLIAdapter
from .schema import structured_out_json_schema
from .model_config import ModelConfig

class CodexCLIAdapter(CLIAdapter):
    executable = "codex"

    def __init__(self, *args: Any, model_config: ModelConfig | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.model_config = model_config

    def _command(self, task: Mapping[str, Any]) -> Sequence[str]:
        schema = structured_out_json_schema()
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(schema, handle)
        self._schema_path = Path(handle.name)
        command = [self._binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-schema", str(self._schema_path)]
        tier = task.get("model_tier")
        if tier is not None:
            if self.model_config is None:
                raise RuntimeError("model_tier was supplied without ModelConfig")
            command.extend(["--model", self.model_config.resolve(str(tier))])
        return command

    def _streaming_command(self, task: Mapping[str, Any]) -> Sequence[str]:
        return [*self._command(task), "--json"]

    def execute(self, task: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return super().execute(task)
        finally:
            path = getattr(self, "_schema_path", None)
            if path is not None:
                path.unlink(missing_ok=True)