"""Deterministic local risk classification for tool operations."""
from __future__ import annotations

import shlex
from collections.abc import Sequence
from enum import StrEnum


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def shell_arguments(command: str | Sequence[str]) -> list[str]:
    """Parse a command without invoking a shell."""
    return shlex.split(command, posix=False) if isinstance(command, str) else list(command)


def classify_shell(command: str | Sequence[str]) -> RiskLevel:
    """Classify the command patterns named in the v0.2 safety specification."""
    arguments = shell_arguments(command)
    normalized = [argument.lower() for argument in arguments]
    joined = " ".join(normalized)
    executable = normalized[0].removesuffix(".exe") if normalized else ""

    if (
        executable in {"rm", "del", "erase", "rmdir", "rd", "format", "shutdown", "reboot"}
        or (executable == "git" and "reset" in normalized and "--hard" in normalized)
        or (executable == "git" and "push" in normalized and ({"--force", "-f"} & set(normalized)))
        or "drop table" in joined
    ):
        return RiskLevel.HIGH
    if executable in {"pip", "pip3", "npm", "winget"} and "install" in normalized:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
