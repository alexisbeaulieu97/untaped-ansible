"""Typer app composition for Ansible dependency graphing."""

from __future__ import annotations

import typer

from untaped_ansible.cli.alias_commands import app as alias_app
from untaped_ansible.cli.graph_commands import register_graph_command
from untaped_ansible.cli.source_commands import app as source_app

app = typer.Typer(name="ansible", help="Analyze Ansible dependency graphs.", no_args_is_help=True)


@app.callback()
def _callback() -> None:
    """Analyze Ansible dependency graphs."""


app.add_typer(alias_app, name="alias")
app.add_typer(source_app, name="source")
register_graph_command(app)
