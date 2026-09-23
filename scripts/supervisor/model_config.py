"""External tier-to-backend-model mapping."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

class ModelConfigError(ValueError):
    pass

@dataclass(frozen=True)
class ModelConfig:
    codex: Mapping[str, str]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ModelConfig":
        codex = value.get("codex")
        if not isinstance(codex, Mapping):
            raise ModelConfigError("codex model mapping is required")
        normalized = {str(key).lower(): str(model) for key, model in codex.items() if isinstance(model, str) and model}
        missing = {"luna", "terra", "sol"} - set(normalized)
        if missing:
            raise ModelConfigError(f"missing model tiers: {sorted(missing)}")
        return cls(normalized)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "ModelConfig":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ModelConfig":
        values = os.environ if environ is None else environ
        return cls.from_mapping({"codex": {"luna": values.get("SUPERVISOR_CODEX_LUNA_MODEL", ""),
                                             "terra": values.get("SUPERVISOR_CODEX_TERRA_MODEL", ""),
                                             "sol": values.get("SUPERVISOR_CODEX_SOL_MODEL", "")}})

    def resolve(self, tier: str) -> str:
        try:
            return self.codex[tier.lower()]
        except KeyError as exc:
            raise ModelConfigError(f"unconfigured model tier: {tier}") from exc