"""Alias management commands for the Ansible plugin."""

from __future__ import annotations

import typer
from untaped import ColumnsOption, FormatOption, UntapedError, format_output, report_errors

from untaped_ansible.infrastructure import AliasRepository

app = typer.Typer(name="alias", help="Manage dependency aliases.", no_args_is_help=True)


@app.command("add", no_args_is_help=True)
def alias_add_command(alias: str, repo: str) -> None:
    """Map an Ansible role/Galaxy name to a GitHub owner/repo."""
    with report_errors():
        AliasRepository().set(alias, repo)
        typer.echo(f"set alias {alias!r} -> {repo}", err=True)


@app.command("list")
def alias_list_command(fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List dependency aliases."""
    with report_errors():
        rows: list[dict[str, object]] = [
            {"alias": alias, "repo": repo}
            for alias, repo in sorted(AliasRepository().entries().items())
        ]
        typer.echo(format_output(rows, fmt=fmt, columns=columns))


@app.command("remove", no_args_is_help=True)
def alias_remove_command(alias: str) -> None:
    """Remove a dependency alias."""
    with report_errors():
        removed = AliasRepository().remove(alias)
        if not removed:
            raise UntapedError(f"unknown alias: {alias!r}")
        typer.echo(f"removed alias {alias!r}", err=True)
