"""Alias management commands for the Ansible plugin."""

from __future__ import annotations

from typing import Annotated

from cyclopts import Parameter
from untaped.api import (
    ColumnsOption,
    FormatOption,
    UntapedError,
    create_app,
    echo,
    render_rows,
    report_errors,
)

from untaped_ansible.infrastructure import AliasRepository

app = create_app(name="alias", help="Manage dependency aliases.")


@app.command(name="add")
def alias_add_command(
    alias: Annotated[str, Parameter(help="Alias to set.")],
    repo: Annotated[str, Parameter(help="Canonical GitHub owner/repo.")],
) -> None:
    """Map an Ansible role/Galaxy name to a GitHub owner/repo."""
    with report_errors():
        AliasRepository().set(alias, repo)
        echo(f"set alias {alias!r} -> {repo}", err=True)


@app.command(name="list")
def alias_list_command(*, fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List dependency aliases."""
    with report_errors():
        rows: list[dict[str, object]] = [
            {"alias": alias, "repo": repo}
            for alias, repo in sorted(AliasRepository().entries().items())
        ]
        rendered = render_rows(
            rows,
            fmt=fmt,
            columns=columns,
            empty="No dependency aliases configured. Map one with "
            "`untaped ansible alias add <name> <repo>`.",
        )
        if rendered:
            echo(rendered)


@app.command(name="remove")
def alias_remove_command(alias: Annotated[str, Parameter(help="Alias to remove.")]) -> None:
    """Remove a dependency alias."""
    with report_errors():
        removed = AliasRepository().remove(alias)
        if not removed:
            raise UntapedError(f"unknown alias: {alias!r}")
        echo(f"removed alias {alias!r}", err=True)
