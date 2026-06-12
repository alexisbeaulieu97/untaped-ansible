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


def test_source_definition_defaults_to_no_explicit_ref_filters() -> None:
    source = SourceDefinition(name="prod", repos=["acme/site"])

    assert source.ref_kinds == []
    assert source.ref_patterns == []


def test_source_definition_allows_tag_scans_without_ref_pattern() -> None:
    source = SourceDefinition(name="prod", repos=["acme/site"], ref_kinds=["tags"])

    assert source.ref_kinds == ["tags"]
    assert source.ref_patterns == []


def test_ansible_settings_default_to_git_source_refresh() -> None:
    settings = AnsibleSettings()

    assert settings.ref_scan_default == "all"
    assert settings.repo_cache_path == Path("~/.untaped/ansible-repositories")
    assert settings.git_clone_protocol == "https"
    assert settings.git_fetch_depth == 1
    assert settings.git_fetch_concurrency == 8
    assert settings.probe_concurrency == 8
    assert settings.git_blob_filter is True


def test_ansible_settings_freshness_ttl_defaults_off_and_rejects_negative_values() -> None:
    assert AnsibleSettings().freshness_ttl is None
    assert AnsibleSettings(freshness_ttl=0).freshness_ttl == 0
    assert AnsibleSettings(freshness_ttl=3600).freshness_ttl == 3600
    with pytest.raises(ValidationError, match="freshness_ttl"):
        AnsibleSettings(freshness_ttl=-1)


def test_ansible_settings_validate_ref_scan_and_git_options() -> None:
    with pytest.raises(ValidationError, match="ref_scan_default"):
        AnsibleSettings(ref_scan_default="main")
    with pytest.raises(ValidationError, match="git_clone_protocol"):
        AnsibleSettings(git_clone_protocol="ftp")
    with pytest.raises(ValidationError, match="git_fetch_depth"):
        AnsibleSettings(git_fetch_depth=-1)
    with pytest.raises(ValidationError, match="git_fetch_concurrency"):
        AnsibleSettings(git_fetch_concurrency=0)
    with pytest.raises(ValidationError, match="git_fetch_concurrency"):
        AnsibleSettings(git_fetch_concurrency=33)
    with pytest.raises(ValidationError, match="probe_concurrency"):
        AnsibleSettings(probe_concurrency=0)
    with pytest.raises(ValidationError, match="probe_concurrency"):
        AnsibleSettings(probe_concurrency=33)
