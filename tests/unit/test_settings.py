"""Tests for Ansible plugin settings defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from untaped_ansible.settings import DEFAULT_DEPENDENCY_PATHS, AnsibleSettings, SourceDefinition


def test_default_dependency_paths_include_yaml_requirements_variants() -> None:
    assert "roles/requirements.yaml" in DEFAULT_DEPENDENCY_PATHS
    assert "requirements.yaml" in DEFAULT_DEPENDENCY_PATHS
    assert "meta/requirements.yaml" in DEFAULT_DEPENDENCY_PATHS


def test_source_definition_defaults_to_branch_scans_only() -> None:
    source = SourceDefinition(name="prod", repos=["acme/site"])

    assert source.ref_kinds == ["heads"]


def test_source_definition_requires_explicit_tag_ref_pattern() -> None:
    with pytest.raises(ValidationError, match="tag scans require --ref-pattern"):
        SourceDefinition(name="prod", repos=["acme/site"], ref_kinds=["tags"])


def test_ansible_settings_default_to_git_backed_cache() -> None:
    settings = AnsibleSettings()

    assert settings.cache_backend == "git"
    assert settings.repo_cache_path == Path("~/.untaped/ansible-repositories")
    assert settings.git_clone_protocol == "https"
    assert settings.git_fetch_depth == 1
    assert settings.git_blob_filter is True


def test_ansible_settings_validate_cache_backend_and_git_options() -> None:
    with pytest.raises(ValidationError, match="cache_backend"):
        AnsibleSettings(cache_backend="graphql")
    with pytest.raises(ValidationError, match="git_clone_protocol"):
        AnsibleSettings(git_clone_protocol="ftp")
    with pytest.raises(ValidationError, match="git_fetch_depth"):
        AnsibleSettings(git_fetch_depth=-1)
