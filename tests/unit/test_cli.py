"""CLI tests for the Ansible plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
import yaml
from typer.testing import CliRunner
from untaped.settings import get_settings

from untaped_ansible import app
from untaped_ansible.application.ports import IndexedDependency
from untaped_ansible.infrastructure import IndexScan, SqliteDependencyIndex


def _write_config(
    tmp_path: Path,
    *,
    index_path: Path | None = None,
    extra_profile: dict[str, object] | None = None,
    top_level_ansible: dict[str, object] | None = None,
) -> Path:
    cfg = tmp_path / "config.yml"
    profile = {
        "ansible": {
            "index_path": str(index_path or tmp_path / "index.sqlite3"),
            "stale_after": 86400,
        }
    }
    if extra_profile:
        profile.update(extra_profile)
    data: dict[str, object] = {"profiles": {"default": profile}}
    if top_level_ansible is not None:
        data["ansible"] = top_level_ansible
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    return cfg


def test_alias_add_list_remove_updates_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(app, ["alias", "add", "common", "acme/common"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["alias", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"alias": "common", "repo": "acme/common"}]

    result = runner.invoke(app, ["alias", "remove", "common"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text()).get("ansible", {}).get("aliases") is None


def test_scope_add_show_remove_updates_config(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "scope",
            "add",
            "prod",
            "--org",
            "acme",
            "--team",
            "platform",
            "--repo",
            "acme/site",
            "--path",
            "deploy/requirements.yml",
            "--ref-kind",
            "heads",
            "--ref-pattern",
            "release/*",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["scope", "show", "prod", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "name": "prod",
            "orgs": ["acme"],
            "teams": ["acme/platform"],
            "repos": ["acme/site"],
            "dependency_paths": ["deploy/requirements.yml"],
            "ref_kinds": ["heads"],
            "ref_patterns": ["release/*"],
        }
    ]

    result = runner.invoke(app, ["scope", "remove", "prod"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text()).get("ansible", {}).get("scopes") is None


def test_graph_command_reads_index_and_renders_mermaid(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_scope_scan(
        IndexScan(
            scope="prod",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v1",
                    source_path="roles/requirements.yml",
                ),
                IndexedDependency(
                    source_repo="acme/base",
                    source_ref="v1",
                    dependency_repo="acme/users",
                    dependency_name="users",
                    dependency_version="main",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["graph", "acme/base", "--ref", "v1", "--scope", "prod", "--format", "mermaid"],
    )

    assert result.exit_code == 0, result.output
    assert "graph LR" in result.stdout
    assert "acme_base_v1 --> acme_users_main" in result.stdout
    assert "acme_site_main --> acme_base_v1" in result.stdout


def test_graph_command_without_ref_reads_indexed_repo_refs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_scope_scan(
        IndexScan(
            scope="prod",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    source_sha="sha-main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v1",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["graph", "acme/site", "--scope", "prod", "--direction", "deps"],
    )

    assert result.exit_code == 0, result.output
    assert "|   +-- acme/base@v1" in result.stdout


def test_graph_command_fetches_remote_target_dependencies_from_github(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        index_path=tmp_path / "index.sqlite3",
        extra_profile={"github": {"token": "ghp_test"}},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/acme/site").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        mock.get("/repos/acme/site/git/trees/main").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "roles/requirements.yml", "type": "blob"},
                        {"path": "meta/main.yml", "type": "blob"},
                    ]
                },
            )
        )
        mock.get("/repos/acme/site/contents/roles/requirements.yml").mock(
            return_value=httpx.Response(
                200,
                text="""
                roles:
                  - src: https://github.com/acme/base
                    version: v1
                """,
            )
        )
        mock.get("/repos/acme/site/contents/meta/main.yml").mock(
            return_value=httpx.Response(
                200,
                text="""
                dependencies:
                  - src: https://github.com/acme/common
                """,
            )
        )

        result = CliRunner().invoke(
            app,
            ["graph", "https://github.com/acme/site", "--direction", "deps", "--depth", "1"],
        )

    assert result.exit_code == 0, result.output
    assert "acme/site" in result.stdout
    assert "|   +-- acme/base@v1" in result.stdout
    assert "|   +-- acme/common" in result.stdout


def test_graph_command_parses_local_target_dependencies(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "role"
    (target / "roles").mkdir(parents=True)
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text(
        '[remote "origin"]\n  url = https://github.com/acme/base.git\n'
    )
    (target / "roles" / "requirements.yml").write_text("- src: https://github.com/acme/users\n")
    cfg = _write_config(tmp_path, index_path=tmp_path / "index.sqlite3")
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", str(target), "--direction", "deps"])

    assert result.exit_code == 0, result.output
    assert "acme/base" in result.stdout
    assert "|   +-- acme/users" in result.stdout


def test_graph_command_infers_repo_from_gitdir_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "role"
    gitdir = tmp_path / "actual-git"
    target.mkdir()
    gitdir.mkdir()
    (target / ".git").write_text(f"gitdir: {gitdir}\n")
    (gitdir / "config").write_text('[remote "origin"]\n  url = https://github.com/acme/base.git\n')
    cfg = _write_config(tmp_path, index_path=tmp_path / "index.sqlite3")
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", str(target), "--direction", "deps"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "acme/base\n"


def test_graph_command_to_ref_uses_new_ref_for_deps_and_old_ref_for_impact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_scope_scan(
        IndexScan(
            scope="prod",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/base",
                    source_ref="v2",
                    dependency_repo="acme/users",
                    dependency_name="users",
                    dependency_version="main",
                    source_path="roles/requirements.yml",
                ),
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v1",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--ref",
            "v1",
            "--to-ref",
            "v2",
            "--scope",
            "prod",
            "--format",
            "mermaid",
        ],
    )

    assert result.exit_code == 0, result.output
    assert 'acme_base_v2["acme/base@v2"]' in result.stdout
    assert "acme_base_v2 --> acme_users_main" in result.stdout
    assert "acme_site_main --> acme_base_v1" in result.stdout


def test_index_status_and_clear(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_scope_scan(
        IndexScan(
            scope="prod",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v1",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(app, ["index", "status", "--scope", "prod", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["edges"] == 1

    result = runner.invoke(app, ["index", "clear", "--scope", "prod"])
    assert result.exit_code == 0, result.output
    assert SqliteDependencyIndex(index_path).status("prod") is None


def test_index_refresh_scans_scope_with_github_client(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={
            "scopes": [
                {
                    "name": "prod",
                    "repos": ["acme/site"],
                    "ref_kinds": ["heads"],
                    "ref_patterns": ["main"],
                }
            ],
            "aliases": {"common": "acme/common"},
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/acme/site/git/matching-refs/heads").mock(
            return_value=httpx.Response(
                200,
                json=[{"ref": "refs/heads/main", "object": {"sha": "abc"}}],
            )
        )
        mock.get("/repos/acme/site/git/trees/abc").mock(
            return_value=httpx.Response(
                200,
                json={"tree": [{"path": "roles/requirements.yml", "type": "blob"}]},
            )
        )
        mock.get("/repos/acme/site/contents/roles/requirements.yml").mock(
            return_value=httpx.Response(200, text="- common\n")
        )
        result = CliRunner().invoke(app, ["index", "refresh", "--scope", "prod"])

    assert result.exit_code == 0, result.output
    assert "refreshed scope 'prod': 1 repos, 1 refs, 1 edges" in result.stderr
    assert SqliteDependencyIndex(index_path).dependents("acme/common", None, scope="prod")


def teardown_module() -> None:
    get_settings.cache_clear()
