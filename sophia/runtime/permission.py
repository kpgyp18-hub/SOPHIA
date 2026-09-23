"""Permission-level policy for local S.O.P.H.I.A tool operations."""
from __future__ import annotations

from collections.abc import Callable
from enum import IntEnum


class PermissionLevel(IntEnum):
    READ = 0
    SAFE_WRITE = 1
    MODIFY = 2
    DESTRUCTIVE = 3


class PermissionDenied(PermissionError):
    """A requested operation violates the local permission policy."""


ApprovalCallback = Callable[[str], bool]


def require_approval(approval: ApprovalCallback | None, description: str) -> None:
    """LEVEL 3 is denied unless an interactive caller explicitly approves it."""
    if approval is None or not approval(description):
        raise PermissionDenied(f"LEVEL 3 approval required: {description}")
