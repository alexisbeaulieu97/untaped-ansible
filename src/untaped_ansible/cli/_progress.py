"""In-place stderr progress reporting for long-running commands."""

from __future__ import annotations

import sys
import time
from typing import TextIO

_DEFAULT_INTERVAL = 2.0
_DEFAULT_PERCENT_STEP = 10


class StderrProgress:
    """Render progress on stderr without touching data-only stdout.

    On a TTY each update rewrites one line in place with ``\\r``. On
    non-TTY streams updates are throttled to one line roughly every
    ``interval`` seconds or every ``percent_step`` percent of completion,
    so piped/CI output stays readable.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        interval: float = _DEFAULT_INTERVAL,
        percent_step: int = _DEFAULT_PERCENT_STEP,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        isatty = getattr(self._stream, "isatty", None)
        self._isatty = bool(isatty()) if callable(isatty) else False
        self._interval = interval
        self._percent_step = max(1, percent_step)
        self._last_emit: float | None = None
        self._last_step = -1
        self._line_width = 0

    def update(self, message: str, *, fraction: float | None = None) -> None:
        """Report progress; ``fraction`` is completion in ``[0, 1]`` if known."""
        if self._isatty:
            padded = message.ljust(self._line_width)
            self._line_width = len(message)
            self._stream.write(f"\r{padded}")
            self._stream.flush()
            return
        step = self._last_step
        if fraction is not None:
            step = int(min(max(fraction, 0.0), 1.0) * 100) // self._percent_step
        now = time.monotonic()
        due = self._last_emit is None or now - self._last_emit >= self._interval
        if not due and step <= self._last_step:
            return
        self._last_emit = now
        self._last_step = max(step, self._last_step)
        self._stream.write(f"{message}\n")
        self._stream.flush()

    def reset_throttle(self) -> None:
        """Restart non-TTY throttling, e.g. when a new phase begins."""
        self._last_emit = None
        self._last_step = -1

    def finish(self) -> None:
        """End any in-place line so following output starts cleanly."""
        if self._isatty and self._line_width:
            self._stream.write("\n")
            self._stream.flush()
        self._line_width = 0
        self.reset_throttle()
