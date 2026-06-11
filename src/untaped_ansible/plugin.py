"""Untaped plugin registration for Ansible dependency graphing."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from untaped.api import CliSpec, PluginManifest, SkillSpec

from untaped_ansible.settings import AnsibleSettings, AnsibleState


class AnsiblePlugin:
    id = "ansible"
    untaped_api_version = 3

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            clis=(
                CliSpec(
                    name="ansible",
                    import_path="untaped_ansible.cli:app",
                    help="Analyze Ansible dependency graphs.",
                ),
            ),
            profile_settings={"ansible": AnsibleSettings},
            state_settings={"ansible": AnsibleState},
            skills=(
                SkillSpec(
                    name="untaped-ansible",
                    source=Path(
                        str(files("untaped_ansible").joinpath("skills", "untaped-ansible"))
                    ),
                    description="Use the untaped Ansible plugin.",
                ),
            ),
        )


plugin = AnsiblePlugin()
