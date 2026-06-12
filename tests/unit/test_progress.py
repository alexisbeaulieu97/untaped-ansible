"""Tests for in-place stderr progress reporting."""

from __future__ import annotations

import io

from untaped_ansible.cli._progress import StderrProgress


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_tty_progress_rewrites_in_place_and_finishes_line() -> None:
    stream = TtyStringIO()
    progress = StderrProgress(stream=stream)

    progress.update("probing refs: 1/2 repos", fraction=0.5)
    progress.update("probing refs: 2/2 repos", fraction=1.0)
    progress.finish()

    assert stream.getvalue() == ("\rprobing refs: 1/2 repos\rprobing refs: 2/2 repos\n")


def test_tty_progress_pads_shorter_lines_to_clear_leftovers() -> None:
    stream = TtyStringIO()
    progress = StderrProgress(stream=stream)

    progress.update("a longer progress line")
    progress.update("short")

    assert "\rshort                 " in stream.getvalue()


def test_non_tty_progress_throttles_to_percent_steps() -> None:
    stream = io.StringIO()
    progress = StderrProgress(stream=stream, interval=3600.0)

    total = 100
    for done in range(1, total + 1):
        progress.update(f"probing refs: {done}/{total} repos", fraction=done / total)
    progress.finish()

    lines = stream.getvalue().splitlines()
    assert lines[0] == "probing refs: 1/100 repos"
    assert "probing refs: 100/100 repos" in lines
    assert len(lines) <= 12


def test_non_tty_progress_emits_after_interval_without_fraction(monkeypatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr("untaped_ansible.cli._progress.time.monotonic", lambda: clock["now"])
    stream = io.StringIO()
    progress = StderrProgress(stream=stream, interval=2.0)

    progress.update("working")
    progress.update("still working")
    clock["now"] = 2.5
    progress.update("nearly there")

    assert stream.getvalue().splitlines() == ["working", "nearly there"]
