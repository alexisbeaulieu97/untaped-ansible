"""Cyclopts app composition for Ansible dependency graphing."""

from __future__ import annotations

from untaped.api import create_app

from untaped_ansible.cli.alias_commands import app as alias_app
from untaped_ansible.cli.graph_commands import register_graph_command
from untaped_ansible.cli.source_commands import app as source_app

app = create_app(name="ansible", help="Analyze Ansible dependency graphs.")


app.command(alias_app, name="alias")
app.command(source_app, name="source")
register_graph_command(app)
