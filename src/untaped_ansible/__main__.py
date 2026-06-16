"""Console-script entrypoint for the ``untaped-ansible`` CLI.

``untaped-ansible`` is a standalone tool built on the untaped SDK. ``main()``
hands the Ansible cyclopts app and a :class:`ToolSpec` (declaring both the
profile settings and the disjoint tool-managed state) to ``run_tool``, which
mounts the shared ``config`` / ``profile`` / ``skills`` groups and runs under
the SDK's error contract.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from untaped.api import SkillAsset, ToolSpec, run_tool

from untaped_ansible.cli import app
from untaped_ansible.settings import AnsibleSettings, AnsibleState

SPEC = ToolSpec(
    command="untaped-ansible",
    section="ansible",
    profile_model=AnsibleSettings,
    state_model=AnsibleState,
    skills=(
        SkillAsset(
            name="untaped-ansible",
            source=Path(str(files("untaped_ansible").joinpath("skills", "untaped-ansible"))),
            description="Use the untaped-ansible CLI.",
        ),
    ),
)


def main() -> object:
    """Run the ``untaped-ansible`` CLI."""
    return run_tool(app, SPEC)


if __name__ == "__main__":
    main()
