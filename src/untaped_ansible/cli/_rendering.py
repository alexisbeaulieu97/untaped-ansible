"""Row rendering helpers for Ansible CLI commands."""

from __future__ import annotations

from collections.abc import Sequence

from untaped import OutputFormat, UiContext, ui_context

Row = dict[str, object]


def render_rows(
    rows: Sequence[Row],
    *,
    fmt: OutputFormat,
    columns: list[str] | None = None,
) -> str:
    """Render row-style CLI output with theme-aware tables only."""
    if fmt == "table":
        return ui_context().collection(rows, fmt=fmt, columns=columns)
    return UiContext().collection(rows, fmt=fmt, columns=columns)
