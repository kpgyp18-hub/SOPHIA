"""Lightweight "still working" progress indicator for blocking agent CLI calls.

Claude/Codex CLI turns (chat_adapter.py, cli_adapter.py) block the calling thread
for the full subprocess duration -- tens of seconds is normal. Without any
feedback, a long turn looks indistinguishable from a hang. WaitProgress shows an
indeterminate tqdm bar (elapsed time, ticking) in a background thread while the
blocking call runs, and gets closed the moment real output starts arriving (or
the call returns, for non-streaming turns) so it never overlaps the agent's
actual reply text.

Uses tqdm because it already handles carriage-return line redraw and terminal
width correctly; degrades to a silent no-op if tqdm isn't installed so this is
never a hard dependency for chat/agents functionality.
"""
from __future__ import annotations

import sys
import threading
from typing import Any

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover -- exercised only when tqdm is absent
    tqdm = None  # type: ignore[assignment]


def _stderr_is_tty() -> bool:
    """tqdm still writes a line per refresh when stderr isn't a real terminal (piped
    output, captured test runs, log redirection) since there's no cursor to redraw
    in place -- that would spam plain-text logs with one line per tick. Only animate
    when there's an actual terminal to animate on."""
    isatty = getattr(sys.stderr, "isatty", None)
    return bool(isatty and isatty())


class WaitProgress:
    """Context-manager-style indeterminate progress indicator.

    Usage:
        progress = WaitProgress("Claude 응답 대기")
        progress.start()
        try:
            ... blocking call ...
        finally:
            progress.stop()

    stop() is idempotent and safe to call more than once (e.g. once from an
    on_line hook when the first output fragment arrives, and again in a
    `finally` block as a backstop).
    """

    def __init__(self, desc: str, *, interval: float = 0.2, enabled: bool = True) -> None:
        self._desc = desc
        self._interval = interval
        self._enabled = enabled and tqdm is not None and _stderr_is_tty()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._bar: Any = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._bar = tqdm(total=0, desc=self._desc, bar_format="{desc} | {elapsed} 경과",
                             leave=False, dynamic_ncols=True)
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()

    def _tick(self) -> None:
        while not self._stop_event.wait(self._interval):
            bar = self._bar
            if bar is not None:
                bar.refresh()

    def stop(self) -> None:
        with self._lock:
            thread, bar = self._thread, self._bar
            self._thread, self._bar = None, None
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=1.0)
        if bar is not None:
            bar.close()
