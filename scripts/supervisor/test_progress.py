"""Tests for the WaitProgress indicator (scripts/supervisor/progress.py)."""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from scripts.supervisor import progress as progress_module
from scripts.supervisor.progress import WaitProgress


class _FakeTTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


class WaitProgressTests(unittest.TestCase):
    def test_disabled_when_stderr_is_not_a_tty(self) -> None:
        # The default test runner's stderr is a captured pipe, not a real terminal.
        wp = WaitProgress("waiting")
        self.assertFalse(wp._enabled)
        wp.start()
        self.assertIsNone(wp._thread)
        wp.stop()  # must not raise even though nothing was started

    def test_enabled_flag_forces_disabled_even_on_a_tty(self) -> None:
        with patch.object(progress_module.sys, "stderr", _FakeTTYStream()):
            wp = WaitProgress("waiting", enabled=False)
            self.assertFalse(wp._enabled)

    def test_starts_and_stops_cleanly_on_a_tty(self) -> None:
        with patch.object(progress_module.sys, "stderr", _FakeTTYStream()):
            wp = WaitProgress("waiting", interval=0.01)
            wp.start()
            self.assertIsNotNone(wp._thread)
            wp.stop()
            self.assertIsNone(wp._thread)
            self.assertIsNone(wp._bar)

    def test_stop_is_idempotent(self) -> None:
        with patch.object(progress_module.sys, "stderr", _FakeTTYStream()):
            wp = WaitProgress("waiting", interval=0.01)
            wp.start()
            wp.stop()
            wp.stop()  # second call must be a harmless no-op

    def test_noop_when_tqdm_missing(self) -> None:
        with patch.object(progress_module.sys, "stderr", _FakeTTYStream()):
            with patch.object(progress_module, "tqdm", None):
                wp = WaitProgress("waiting")
                self.assertFalse(wp._enabled)
                wp.start()
                self.assertIsNone(wp._thread)
                wp.stop()


if __name__ == "__main__":
    unittest.main()
