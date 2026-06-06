"""Untaped plugin registration for Ansible dependency graphing."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from untaped.plugins import PluginRegistry, SkillSpec

from untaped_ansible import app
from untaped_ansible.settings import AnsibleSettings, AnsibleState


class AnsiblePlugin:
    id = "ansible"
    untaped_api_version = 1

    def register(self, registry: PluginRegistry) -> None:
        registry.add_profile_settings("ansible", AnsibleSettings)
        registry.add_state_settings("ansible", AnsibleState)
        registry.add_cli("ansible", app)
        registry.add_skill(
            SkillSpec(
                name="untaped-ansible",
                source=Path(str(files("untaped_ansible").joinpath("skills", "untaped-ansible"))),
                description="Use the untaped Ansible plugin.",
            )
        )


plugin = AnsiblePlugin()
