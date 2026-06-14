"""Tests for untaped plugin registration."""

from __future__ import annotations

import subprocess
import sys

from untaped.api import PluginManifest, PluginRegistry
from untaped.plugins import register_plugins

from untaped_ansible.plugin import plugin
from untaped_ansible.settings import AnsibleSettings, AnsibleState


def test_plugin_declares_untaped_api_version_5() -> None:
    assert plugin.id == "ansible"
    assert plugin.untaped_api_version == 5


def test_manifest_declares_lazy_cli_settings_and_skill() -> None:
    manifest = plugin.manifest()

    assert isinstance(manifest, PluginManifest)
    (cli,) = manifest.clis
    assert cli.name == "ansible"
    assert cli.app is None
    assert cli.import_path == "untaped_ansible.cli:app"
    assert cli.help
    assert manifest.profile_settings == {"ansible": AnsibleSettings}
    assert manifest.state_settings == {"ansible": AnsibleState}
    (skill,) = manifest.skills
    assert skill.name == "untaped-ansible"
    assert skill.description == "Use the untaped Ansible plugin."
    assert skill.source.joinpath("SKILL.md").is_file()


def test_register_plugins_commits_manifest_atomically() -> None:
    registry = PluginRegistry()

    register_plugins(registry, [plugin])

    assert registry.load_errors == []
    assert "ansible" in registry.plugin_ids
    assert "ansible" in registry.lazy_clis
    assert "ansible" in registry.profile_sections
    assert "ansible" in registry.state_sections
    assert "untaped-ansible" in registry.skills


def test_plugin_discovery_does_not_import_cli_modules() -> None:
    code = (
        "import sys\n"
        "import untaped_ansible\n"
        "import untaped_ansible.plugin\n"
        "untaped_ansible.plugin.plugin.manifest()\n"
        "loaded = [m for m in sys.modules if m.startswith('untaped_ansible.cli')]\n"
        "assert not loaded, f'CLI modules imported eagerly: {loaded}'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_package_lazily_re_exports_app() -> None:
    import untaped_ansible
    from untaped_ansible.cli import app as cli_app

    assert untaped_ansible.app is cli_app
