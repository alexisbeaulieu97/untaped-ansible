"""Typed-pipe (`--format pipe`) envelope tests for the Ansible tool.

Each row-producing command must tag its `--format pipe` output with a
namespaced `kind` hint so downstream consumers can route records without
sniffing fields. These tests assert the envelope contract per producer.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from untaped.settings import get_settings
from untaped.testing import CliInvoker

from untaped_ansible import app


def _write_config(
    tmp_path: Path,
    *,
    top_level_ansible: dict[str, object] | None = None,
) -> Path:
    cfg = tmp_path / "config.yml"
    # SDK v2.0.0 profiles layout: the ansible PROFILE fields live under
    # `profiles.default.ansible`; the ansible STATE (sources/aliases) stays
    # top-level under `ansible`.
    ansible_profile_section: dict[str, object] = {
        "index_path": str(tmp_path / "index.sqlite3"),
        "stale_after": 86400,
    }
    data: dict[str, object] = {"profiles": {"default": {"ansible": ansible_profile_section}}}
    if top_level_ansible is not None:
        data["ansible"] = dict(top_level_ansible)
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    return cfg


def test_alias_list_pipe_tags_envelope_with_kind(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path, top_level_ansible={"aliases": {"common": "acme/common"}})
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["alias", "list", "--format", "pipe"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip())
    assert envelope["untaped"] == "1"
    assert envelope["kind"] == "ansible.alias"
    assert envelope["record"]["alias"] == "common"
    assert envelope["record"]["repo"] == "acme/common"


def test_source_list_pipe_tags_envelope_with_kind(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "list", "--format", "pipe"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip())
    assert envelope["untaped"] == "1"
    assert envelope["kind"] == "ansible.source"
    assert envelope["record"]["name"] == "prod"


def test_source_show_pipe_tags_envelope_with_kind(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "show", "prod", "--format", "pipe"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip())
    assert envelope["untaped"] == "1"
    assert envelope["kind"] == "ansible.source"
    assert envelope["record"]["name"] == "prod"


def test_source_status_pipe_tags_envelope_with_kind(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "status", "--format", "pipe"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip())
    assert envelope["untaped"] == "1"
    assert envelope["kind"] == "ansible.source-status"
    assert envelope["record"]["source"] == "prod"
