"""Tests for Ansible plugin settings defaults."""

from __future__ import annotations

from untaped_ansible.settings import DEFAULT_DEPENDENCY_PATHS


def test_default_dependency_paths_include_yaml_requirements_variants() -> None:
    assert "roles/requirements.yaml" in DEFAULT_DEPENDENCY_PATHS
    assert "requirements.yaml" in DEFAULT_DEPENDENCY_PATHS
    assert "meta/requirements.yaml" in DEFAULT_DEPENDENCY_PATHS
