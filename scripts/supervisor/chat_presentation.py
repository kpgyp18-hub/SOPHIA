"""TTY-only visual presentation for the interactive group-chat console."""
from __future__ import annotations

import sys
from typing import Callable, Mapping

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
except ImportError:  # pragma: no cover - optional presentation dependency
    Console = None  # type: ignore[assignment,misc]


_STYLES = {
    "You": ("YOU", "bold magenta"),
    "Claude": ("BLANC · CLAUDE", "bold cyan"),
    "Codex": ("ADE · CODEX", "bold yellow"),
    "System": ("SYSTEM", "dim green"),
}


class ChatPresenter:
    """Render readable, separated chat turns when Rich and a real TTY exist.

    It is deliberately a presentation layer only: transcript files and piped
    output keep the existing plain-text format.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self.enabled = bool(enabled and is_tty and Console is not None)
        self.console = Console() if self.enabled else None
        self._models: dict[str, str | None] = {}

    def set_agent_models(self, models: Mapping[str, str | None]) -> None:
        """Set the model labels shown next to each agent in the live TTY."""
        self._models = dict(models)

    def set_agent_model(self, speaker: str, model: str | None) -> None:
        """Update one displayed model after a /model command."""
        self._models[speaker] = model

    def _title(self, speaker: str) -> tuple[str, str]:
        title, style = _STYLES.get(speaker, (speaker.upper(), "bold white"))
        if speaker in self._models:
            # A blank override delegates selection to the provider; do not imply
            # that the presenter knows the provider's eventual concrete model ID.
            model = self._models[speaker] or "provider default"
            title = f"{title} · {model}"
        return title, style

    def __call__(self, speaker: str, text: str) -> None:
        if not self.enabled or self.console is None:
            print(f"{speaker}: {text}")
            return
        title, style = self._title(speaker)
        self.console.print()
        if speaker == "System":
            self.console.print(Text(f"◆ {title}  {text}", style=style))
            return
        self.console.print(Panel(
            Text(text), title=title, title_align="left", border_style=style,
            box=box.ROUNDED, padding=(0, 1),
        ))

    def stream_prefix(self, speaker: str) -> None:
        """Start a live stream on its own visually distinct line."""
        if not self.enabled or self.console is None:
            print(f"{speaker}: ", end="", flush=True)
            return
        title, style = self._title(speaker)
        self.console.print()
        self.console.print(Text(f"◆ {title}  ", style=style), end="")
