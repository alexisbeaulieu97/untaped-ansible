"""Untaped plugin registration for Ansible dependency graphing."""

from __future__ import annotations

from untaped.plugins import PluginRegistry

from untaped_ansible import app
from untaped_ansible.settings import AnsibleSettings, AnsibleState


class AnsiblePlugin:
    id = "ansible"

    def register(self, registry: PluginRegistry) -> None:
        registry.add_profile_settings("ansible", AnsibleSettings)
        registry.add_state_settings("ansible", AnsibleState)
        registry.add_cli("ansible", app)


plugin = AnsiblePlugin()
