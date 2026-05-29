"""Tests for untaped plugin registration."""

from __future__ import annotations

from untaped.plugins import PluginRegistry

from untaped_ansible.plugin import plugin


def test_plugin_registers_cli_and_settings_sections() -> None:
    registry = PluginRegistry()

    plugin.register(registry)

    assert "ansible" in registry.clis
    assert "ansible" in registry.profile_sections
    assert "ansible" in registry.state_sections
