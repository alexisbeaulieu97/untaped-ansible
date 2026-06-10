"""Tests for untaped plugin registration."""

from __future__ import annotations

from untaped.plugins import PluginRegistry

from untaped_ansible.plugin import plugin


def test_plugin_declares_untaped_api_version() -> None:
    assert plugin.untaped_api_version == 2


def test_plugin_registers_cli_and_settings_sections() -> None:
    registry = PluginRegistry()

    plugin.register(registry)

    assert "ansible" in registry.clis
    assert "ansible" in registry.profile_sections
    assert "ansible" in registry.state_sections
    spec = registry.skills["untaped-ansible"]
    assert spec.description == "Use the untaped Ansible plugin."
    assert spec.source.joinpath("SKILL.md").is_file()
