"""Read-only subprocess transports for explicitly enabled CLI brains."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from sophia.brains.ollama import BrainResult


class CliTransport:
    """Run one CLI inference without inheriting the interactive chat permissions."""

    _providers = {"claude", "codex"}

    def __init__(self, provider: str, *, cwd: str | Path | None = None,
                 timeout_seconds: float = 300.0) -> None:
        if provider not in self._providers:
            raise ValueError(f"unsupported CLI provider: {provider}")
        if timeout_seconds <= 0:
            raise ValueError("CLI timeout_seconds must be positive")
        self.provider = provider
        self.cwd = str(cwd) if cwd is not None else None
        self.timeout_seconds = timeout_seconds

    def __call__(self, prompt: str) -> BrainResult:
        binary = self._resolve_binary()
        started = time.perf_counter_ns()
        try:
            completed = subprocess.run(
                self._command(binary), input=prompt, text=True, capture_output=True,
                cwd=self.cwd, timeout=self.timeout_seconds, check=False,
                encoding="utf-8", errors="replace", env=self._environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise OSError(f"{self.provider} CLI timed out after {self.timeout_seconds:g}s") from error
        duration_ns = time.perf_counter_ns() - started
        if completed.returncode != 0:
            diagnostic = (completed.stderr.strip() or completed.stdout.strip())[-800:]
            raise OSError(f"{self.provider} CLI exited with code {completed.returncode}: {diagnostic}")
        return BrainResult(
            completed.stdout.strip(), prompt_tokens=max(1, len(prompt.split())),
            duration_ns=duration_ns, model=f"{self.provider}-cli", provider=f"{self.provider}-cli",
        )

    def _resolve_binary(self) -> str:
        candidate = shutil.which(f"{self.provider}.cmd") if os.name == "nt" else None
        candidate = candidate or shutil.which(self.provider)
        if not candidate:
            raise OSError(f"{self.provider} CLI not found")
        return candidate

    def _command(self, binary: str) -> list[str]:
        if self.provider == "claude":
            return [binary, "--print", "--permission-mode", "plan"]
        return [binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check"]

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.provider != "codex" or env.get("CODEX_HOME"):
            return env
        profile = env.get("USERPROFILE" if os.name == "nt" else "HOME")
        default_home = Path(profile) / ".codex" if profile else None
        if default_home is not None and default_home.is_dir():
            env["CODEX_HOME"] = str(default_home)
        return env
