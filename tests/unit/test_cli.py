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


def _mock_dependency_file(
    mock: respx.MockRouter,
    repo: str,
    *,
    ref: str = "main",
    sha: str | None = None,
    content: str = "- src: https://github.com/acme/base\n",
) -> None:
    owner, name = repo.split("/", maxsplit=1)
    read_ref = sha or ref
    mock.get(f"/repos/{owner}/{name}").mock(
        return_value=httpx.Response(200, json={"default_branch": ref})
    )
    mock.get(f"/repos/{owner}/{name}/git/trees/{read_ref}").mock(
        return_value=httpx.Response(
            200,
            json={"tree": [{"path": "roles/requirements.yml", "type": "blob"}]},
        )
    )
    mock.get(f"/repos/{owner}/{name}/contents/roles/requirements.yml").mock(
        return_value=httpx.Response(200, text=content)
    )


def _mock_refresh_repo(
    mock: respx.MockRouter,
    repo: str,
    *,
    sha: str,
    content: str,
    refs_path: str = "heads/main",
    default_branch: str | None = "main",
) -> None:
    owner, name = repo.split("/", maxsplit=1)
    if default_branch is not None:
        mock.get(f"/repos/{owner}/{name}").mock(
            return_value=httpx.Response(200, json={"default_branch": default_branch})
        )
    mock.get(f"/repos/{owner}/{name}/git/matching-refs/{refs_path}").mock(
        return_value=httpx.Response(
            200,
            json=[{"ref": "refs/heads/main", "object": {"sha": sha}}],
        )
    )
    mock.get(f"/repos/{owner}/{name}/git/trees/{sha}").mock(
        return_value=httpx.Response(
            200,
            json={"tree": [{"path": "roles/requirements.yml", "type": "blob"}]},
        )
    )
    mock.get(f"/repos/{owner}/{name}/contents/roles/requirements.yml").mock(
        return_value=httpx.Response(200, text=content)
    )


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


def test_source_save_show_remove_updates_config(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "source",
            "save",
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

    result = runner.invoke(app, ["source", "show", "prod", "--format", "json"])
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

    result = runner.invoke(app, ["source", "remove", "prod"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text()).get("ansible", {}).get("sources") is None


def test_graph_downstream_reads_remote_dependencies_without_source(
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
        _mock_dependency_file(mock, "acme/site")
        result = CliRunner().invoke(
            app,
            ["graph", "acme/site", "--downstream", "--depth", "1"],
        )

    assert result.exit_code == 0, result.output
    assert "acme/site" in result.stdout
    assert "|   +-- acme/base" in result.stdout
    assert "upstream omitted" not in result.stdout


def test_graph_inline_upstream_refreshes_and_renders_impact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/orgs/acme/repos").mock(
            return_value=httpx.Response(200, json=[{"full_name": "acme/site"}])
        )
        _mock_refresh_repo(
            mock,
            "acme/site",
            sha="sha-main",
            content="- src: https://github.com/acme/base\n  version: v1\n",
        )
        result = CliRunner().invoke(
            app,
            [
                "graph",
                "acme/base",
                "--ref",
                "v1",
                "--org",
                "acme",
                "--ref-kind",
                "heads",
                "--upstream",
                "--refresh",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "+-- upstream" in result.stdout
    assert "    +-- acme/site@main" in result.stdout
    assert SqliteDependencyIndex(index_path).dependents("acme/base", "v1", source_key=None)


def test_graph_inline_source_reuses_fingerprint_cache_without_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/orgs/acme/repos").mock(
            return_value=httpx.Response(200, json=[{"full_name": "acme/site"}])
        )
        _mock_refresh_repo(
            mock,
            "acme/site",
            sha="sha-main",
            content="- src: https://github.com/acme/base\n",
        )
        first = runner.invoke(
            app,
            [
                "graph",
                "acme/base",
                "--org",
                "acme",
                "--ref-kind",
                "heads",
                "--upstream",
                "--refresh",
            ],
        )

    assert first.exit_code == 0, first.output

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        second = runner.invoke(
            app,
            ["graph", "acme/base", "--org", "acme", "--ref-kind", "heads", "--upstream"],
        )
        assert len(mock.calls) == 0

    assert second.exit_code == 0, second.output
    assert "    +-- acme/site@main" in second.stdout


def test_graph_source_upstream_requires_refresh_when_index_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        index_path=tmp_path / "index.sqlite3",
        top_level_ansible={"sources": [{"name": "platform", "orgs": ["acme"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", "acme/base", "--source", "platform", "--upstream"])

    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output
    assert "untaped ansible source refresh platform" in result.output
    assert "untaped ansible graph acme/base --source platform --upstream --refresh" in result.output


def test_source_save_clears_cached_data_for_redefined_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(
            source_key="source:platform",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/old-site",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/old-site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(app, ["source", "save", "platform", "--repo", "acme/new-site"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["graph", "acme/base", "--source", "platform", "--upstream"])
    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output


def test_source_save_preserves_cached_data_for_identical_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(
            source_key="source:platform",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(app, ["source", "save", "platform", "--repo", "acme/site"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["graph", "acme/base", "--source", "platform", "--upstream"])
    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout


def test_graph_both_renders_downstream_and_warns_when_upstream_unavailable(
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
        _mock_dependency_file(mock, "acme/site")
        result = CliRunner().invoke(app, ["graph", "acme/site", "--both", "--depth", "1"])

    assert result.exit_code == 0, result.output
    assert "|   +-- acme/base" in result.stdout
    assert (
        "warning: only showing downstream; upstream omitted because no source is configured"
        in result.stdout
    )


def test_graph_downstream_with_source_uses_cached_data_without_live_reads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(
            source_key="source:platform",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/cached",
                    dependency_name="cached",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={
            "stale_after": 60,
            "sources": [{"name": "platform", "repos": ["acme/site"]}],
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        result = CliRunner().invoke(
            app,
            ["graph", "acme/site", "--source", "platform", "--downstream", "--depth", "1"],
        )
        assert len(mock.calls) == 0

    assert result.exit_code == 0, result.output
    assert "|   +-- acme/cached" in result.stdout


def test_graph_downstream_with_source_live_flag_reads_remote_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(
            source_key="source:platform",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/cached",
                    dependency_name="cached",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_dependency_file(
            mock,
            "acme/site",
            content="- src: https://github.com/acme/live\n",
        )
        result = CliRunner().invoke(
            app,
            [
                "graph",
                "acme/site",
                "--source",
                "platform",
                "--downstream",
                "--depth",
                "1",
                "--live",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "|   +-- acme/live" in result.stdout
    assert "acme/cached" not in result.stdout


def test_graph_direction_flags_are_mutually_exclusive(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", "acme/site", "--upstream", "--downstream"])

    assert result.exit_code == 2
    assert "choose only one of --upstream, --downstream, or --both" in result.output


def test_graph_source_conflicts_with_inline_selectors(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "platform", "orgs": ["acme"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["graph", "acme/site", "--source", "platform", "--org", "acme"],
    )
    output = " ".join(result.output.replace("│", " ").split())

    assert result.exit_code == 2
    assert "--source cannot be combined with --org, --team, --repo, --path" in output


def test_source_save_validates_search_boundary_repo_and_ref_kind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    cases = [
        (
            ["source", "save", "prod", "--path", "roles/requirements.yml"],
            "requires --org, --team, or --repo",
        ),
        (["source", "save", "prod", "--repo", "not-a-repo"], "repo must be owner/name"),
        (
            ["source", "save", "prod", "--repo", "acme/site", "--ref-kind", "pulls"],
            "ref-kind must be heads or tags",
        ),
    ]

    for args, message in cases:
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert message in result.output


def test_config_loaded_source_uses_same_validation(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "bad", "repos": ["not-a-repo"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["source", "refresh", "bad"])

    assert result.exit_code == 1
    assert "repo must be owner/name" in result.output


def test_inline_source_cache_key_is_order_insensitive(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_repo(
            mock,
            "acme/a",
            sha="sha-a",
            content="- src: https://github.com/acme/base\n",
        )
        _mock_refresh_repo(
            mock,
            "acme/b",
            sha="sha-b",
            content="- src: https://github.com/acme/base\n",
        )
        first = runner.invoke(
            app,
            [
                "graph",
                "acme/base",
                "--repo",
                "acme/a",
                "--repo",
                "acme/b",
                "--ref-kind",
                "heads",
                "--upstream",
                "--refresh",
            ],
        )

    assert first.exit_code == 0, first.output

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        second = runner.invoke(
            app,
            [
                "graph",
                "acme/base",
                "--repo",
                "acme/b",
                "--repo",
                "acme/a",
                "--ref-kind",
                "heads",
                "--upstream",
            ],
        )
        assert len(mock.calls) == 0

    assert second.exit_code == 0, second.output
    assert "acme/a@main" in second.stdout
    assert "acme/b@main" in second.stdout


def test_graph_repo_is_source_selector_and_target_repo_overrides_local_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "role"
    (target / "roles").mkdir(parents=True)
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text(
        '[remote "origin"]\n  url = https://github.com/acme/wrong.git\n'
    )
    (target / "roles" / "requirements.yml").write_text("- src: https://github.com/acme/users\n")
    cfg = _write_config(tmp_path, index_path=tmp_path / "index.sqlite3")
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["graph", str(target), "--target-repo", "acme/base", "--downstream"],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("acme/base\n")
    assert "|   +-- acme/users" in result.stdout


def test_graph_local_target_infers_repo_from_gitdir_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "role"
    gitdir = tmp_path / "gitdir"
    (target / "roles").mkdir(parents=True)
    gitdir.mkdir()
    (target / ".git").write_text(f"gitdir: {gitdir}\n")
    (gitdir / "config").write_text(
        '[remote "origin"]\n  url = git@github.com:acme/worktree-role.git\n'
    )
    (target / "roles" / "requirements.yml").write_text("- src: https://github.com/acme/users\n")
    cfg = _write_config(tmp_path, index_path=tmp_path / "index.sqlite3")
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", str(target), "--downstream"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("acme/worktree-role\n")
    assert "|   +-- acme/users" in result.stdout


def test_graph_local_target_prefers_origin_remote_for_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "role"
    (target / "roles").mkdir(parents=True)
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text(
        '[remote "upstream"]\n'
        "  url = https://github.com/acme/upstream-role.git\n"
        '[remote "origin"]\n'
        "  url = https://github.com/acme/origin-role.git\n"
    )
    (target / "roles" / "requirements.yml").write_text("- src: https://github.com/acme/users\n")
    cfg = _write_config(tmp_path, index_path=tmp_path / "index.sqlite3")
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", str(target), "--downstream"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("acme/origin-role\n")


def test_graph_empty_local_dependency_result_explains_checked_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "role"
    target.mkdir()
    cfg = _write_config(tmp_path, index_path=tmp_path / "index.sqlite3")
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["graph", str(target), "--target-repo", "acme/empty", "--downstream"],
    )

    assert result.exit_code == 0, result.output
    assert "warning: no declared downstream dependencies found for acme/empty" in result.stdout
    assert "checked configured dependency paths" in result.stdout


def test_graph_empty_local_dependency_result_does_not_fall_back_to_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(
            source_key="source:platform",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/empty",
                    source_ref=None,
                    dependency_repo="acme/stale",
                    dependency_name="stale",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    target = tmp_path / "role"
    target.mkdir()
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["graph", str(target), "--target-repo", "acme/empty", "--downstream"],
    )

    assert result.exit_code == 0, result.output
    assert "acme/stale" not in result.stdout
    assert "warning: no declared downstream dependencies found for acme/empty" in result.stdout


def test_graph_output_writes_data_to_file_and_keeps_stdout_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "deps.mmd"
    cfg = _write_config(
        tmp_path,
        index_path=tmp_path / "index.sqlite3",
        extra_profile={"github": {"token": "ghp_test"}},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_dependency_file(mock, "acme/site")
        result = CliRunner().invoke(
            app,
            [
                "graph",
                "acme/site",
                "--downstream",
                "--format",
                "mermaid",
                "--depth",
                "1",
                "--output",
                str(output),
            ],
        )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert output.read_text().startswith("graph LR\n")


def test_graph_alias_resolves_upstream_target(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(
            source_key="source:platform",
            scanned_at=datetime.now(UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [{"name": "platform", "repos": ["acme/site"]}],
            "aliases": {"base": "acme/base"},
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["graph", "base", "--source", "platform", "--upstream"])

    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout


def test_graph_help_teaches_clean_source_first_workflow() -> None:
    result = CliRunner().invoke(app, ["graph", "--help"])
    output = " ".join(result.output.replace("│", " ").split())

    assert result.exit_code == 0, result.output
    assert "--upstream" in output
    assert "--downstream" in output
    assert "--source" in output
    assert "--refresh" in output
    assert "--live" in output
    assert "--target-repo" in output
    assert "--both" in output
    assert "Show upstream and downstream (default)." in output
    assert "Examples:" in output
    assert (
        "untaped ansible graph acme/base --org acme --team platform --upstream --refresh" in output
    )
    assert "--scope" not in output
    assert "--direction" not in output


def test_source_status_classifies_missing_unindexed_and_stale_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    SqliteDependencyIndex(index_path).replace_source_scan(
        IndexScan(source_key="source:stale", scanned_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "stale_after": 60,
            "sources": [
                {"name": "unindexed", "repos": ["acme/site"]},
                {"name": "stale", "repos": ["acme/base"]},
            ],
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliRunner()

    result = runner.invoke(app, ["source", "status", "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = {row["source"]: row for row in json.loads(result.stdout)}
    assert rows["unindexed"]["state"] == "not-refreshed"
    assert rows["stale"]["state"] == "stale"

    result = runner.invoke(app, ["source", "status", "missing", "--format", "json"])
    assert result.exit_code == 1
    assert "unknown source: 'missing'" in result.output


def test_source_refresh_scans_source_with_github_client(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={
            "sources": [
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
        _mock_refresh_repo(
            mock,
            "acme/site",
            sha="abc",
            content="- common\n",
            default_branch=None,
        )
        result = CliRunner().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 0, result.output
    assert "refreshed source 'prod': 1 repos, 1 refs, 1 edges" in result.stderr
    assert SqliteDependencyIndex(index_path).dependents(
        "acme/common", None, source_key="source:prod"
    )


def test_source_save_expands_bare_team_slug_with_single_org(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        ["source", "save", "prod", "--org", "acme", "--team", "platform"],
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text())["ansible"]["sources"][0]["teams"] == ["acme/platform"]


def teardown_module() -> None:
    get_settings.cache_clear()
