"""CLI tests for the Ansible tool."""

from __future__ import annotations

import json
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from untaped.settings import get_settings
from untaped.testing import CliInvoker

from untaped_ansible import app
from untaped_ansible.application.refresh_index import RefreshResult
from untaped_ansible.cli import _refresh
from untaped_ansible.domain.payloads import (
    IndexedDependency,
    RefScan,
    RepoFailure,
    SourceRepoMetadata,
)
from untaped_ansible.infrastructure import SqliteDependencyIndex


def _write_config(
    tmp_path: Path,
    *,
    index_path: Path | None = None,
    extra_profile: dict[str, object] | None = None,
    ansible_profile: dict[str, object] | None = None,
    top_level_ansible: dict[str, object] | None = None,
    top_level_ui: dict[str, object] | None = None,
) -> Path:
    cfg = tmp_path / "config.yml"
    # SDK v2.0.0 profiles layout: user-tunable PROFILE sections (the ansible
    # profile fields, the cross-tool `github` section, `ui`) live under
    # `profiles.default.<section>`. The ansible STATE section (sources/aliases)
    # stays top-level under the `ansible` key.
    ansible_profile_section: dict[str, object] = {
        "index_path": str(index_path or tmp_path / "index.sqlite3"),
        "stale_after": 86400,
    }
    if ansible_profile:
        ansible_profile_section.update(ansible_profile)
    default_profile: dict[str, object] = {"ansible": ansible_profile_section}
    if extra_profile:
        default_profile.update(extra_profile)
    if top_level_ui is not None:
        default_profile["ui"] = top_level_ui
    data: dict[str, object] = {"profiles": {"default": default_profile}}
    if top_level_ansible is not None:
        # State (sources/aliases) is a disjoint top-level `ansible` section.
        data["ansible"] = dict(top_level_ansible)
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


def _graphql_repo_node(repo: str, *, sha: str, default_branch: str = "main") -> dict[str, object]:
    empty_page = {"hasNextPage": False, "endCursor": None}
    return {
        "nameWithOwner": repo,
        "defaultBranchRef": {"name": default_branch},
        "heads": {
            "pageInfo": empty_page,
            "nodes": [{"name": default_branch, "target": {"oid": sha}}],
        },
        "tags": {"pageInfo": empty_page, "nodes": []},
    }


def _mock_refresh_repos(
    mock: respx.MockRouter,
    repos: dict[str, str],
    *,
    missing: tuple[str, ...] = (),
    rate_limit_remaining: int = 4900,
) -> None:
    """Mock source-refresh expansion (REST) and the GraphQL ref probe.

    ``repos`` maps ``owner/name`` to the sha of its single ``heads/main``
    ref; ``missing`` repos expand fine but probe as NOT_FOUND.
    """
    names = sorted([*repos, *missing])
    data: dict[str, object] = {
        "rateLimit": {
            "cost": 1,
            "remaining": rate_limit_remaining,
            "resetAt": "2026-01-01T00:00:00Z",
        }
    }
    errors: list[dict[str, object]] = []
    for index, full_name in enumerate(names):
        alias = f"r{index}"
        owner, name = full_name.split("/", maxsplit=1)
        mock.get(f"/repos/{owner}/{name}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "full_name": full_name,
                    "default_branch": "main",
                    "clone_url": f"https://github.com/{full_name}.git",
                },
            )
        )
        if full_name in missing:
            data[alias] = None
            errors.append(
                {
                    "type": "NOT_FOUND",
                    "path": [alias],
                    "message": f"Could not resolve to a Repository named {full_name!r}.",
                }
            )
            continue
        data[alias] = _graphql_repo_node(full_name, sha=repos[full_name])
    payload: dict[str, object] = {"data": data}
    if errors:
        payload["errors"] = errors
    mock.post("/graphql").mock(return_value=httpx.Response(200, json=payload))


def _mock_refresh_graphql_error(
    mock: respx.MockRouter,
    repos: tuple[str, ...],
    response: httpx.Response,
) -> None:
    """Mock source-refresh expansion, then fail the GraphQL ref probe globally."""
    for full_name in sorted(repos):
        owner, name = full_name.split("/", maxsplit=1)
        mock.get(f"/repos/{owner}/{name}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "full_name": full_name,
                    "default_branch": "main",
                    "clone_url": f"https://github.com/{full_name}.git",
                },
            )
        )
    mock.post("/graphql").mock(return_value=response)


class _SeedGitCache:
    """Git transport stub for seeding: fetches succeed, no dependency files."""

    def ensure_bare(self, url: str, *, cache_dir: Path, auth_header: str | None) -> Path:
        return cache_dir / url.removesuffix(".git").rsplit("/", maxsplit=1)[-1]

    def fetch_refs(
        self,
        bare_path: Path,
        *,
        refspecs: list[str],
        depth: int,
        blob_filter: bool,
        auth_header: str | None,
    ) -> None:
        return None

    def read_file(
        self,
        bare_path: Path,
        sha: str,
        path: str,
        *,
        auth_header: str | None,
    ) -> str | None:
        return None


def _seed_unchanged_scan(
    monkeypatch,
    repos: dict[str, str],
    *,
    missing: tuple[str, ...] = (),
) -> None:
    """Seed scans a later refresh's probe will consider unchanged.

    Runs one ``source refresh prod`` through the public CLI (with the git
    transport stubbed out) so the cached scans carry exactly the metadata and
    fingerprints a subsequent refresh recomputes.
    """
    with monkeypatch.context() as patcher:
        patcher.setattr(_refresh, "GitRepositoryCache", _SeedGitCache)
        with respx.mock(base_url="https://api.github.com") as mock:
            _mock_refresh_repos(mock, repos, missing=missing)
            result = CliInvoker().invoke(app, ["source", "refresh", "prod"])
    assert result.exit_code == (1 if missing else 0), result.output


def _seed_index(
    index: SqliteDependencyIndex,
    source_key: str,
    dependencies: tuple[IndexedDependency, ...] = (),
    *,
    scanned_at: datetime | None = None,
    repo_metadata: tuple[SourceRepoMetadata, ...] = (),
) -> None:
    now = scanned_at or datetime.now(UTC)
    grouped: dict[tuple[str, str, str], list[IndexedDependency]] = {}
    for edge in dependencies:
        grouped.setdefault(
            (edge.source_repo, edge.source_ref or "main", edge.source_ref_kind or "heads"),
            [],
        ).append(edge)
    if not grouped:
        grouped[("acme/site", "main", "heads")] = []
    scans = tuple(
        RefScan(
            source_key=source_key,
            source_repo=source_repo,
            ref_kind=ref_kind,
            source_ref=source_ref,
            source_sha=next(
                (edge.source_sha for edge in edges if edge.source_sha is not None),
                f"sha-{source_ref}",
            ),
            clone_url=f"https://github.com/{source_repo}.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=now,
            indexed_at=now,
            dependencies=tuple(edges),
        )
        for (source_repo, source_ref, ref_kind), edges in grouped.items()
    )
    index.commit_source_ref_refresh(
        source_key,
        scans=scans,
        touches=(),
        keep={(scan.source_repo, scan.ref_kind, scan.source_ref) for scan in scans},
        repo_metadata=repo_metadata,
        scanned_at=now,
    )


def test_alias_add_list_remove_updates_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

    result = runner.invoke(app, ["alias", "add", "common", "acme/common"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["alias", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"alias": "common", "repo": "acme/common"}]

    result = runner.invoke(app, ["alias", "remove", "common"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text()).get("ansible", {}).get("aliases") is None


def test_alias_list_table_honours_global_collection_view_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"aliases": {"common": "acme/common"}},
        top_level_ui={"collection_view": "list"},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["alias", "list", "--format", "table"])

    assert result.exit_code == 0, result.output
    assert "alias: common" in result.stdout
    assert "repo: acme/common" in result.stdout
    assert "╭" not in result.stdout
    assert "┌" not in result.stdout


def test_alias_list_raw_ignores_invalid_global_theme(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"aliases": {"common": "acme/common"}},
        top_level_ui={"theme": "missing"},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["alias", "list", "--format", "raw"])

    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert result.stdout.splitlines() == ["common"]


def test_source_save_show_remove_updates_config(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

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
    # A single source renders as a bare object {…} via emit (not [{…}]).
    assert json.loads(result.stdout) == {
        "name": "prod",
        "orgs": ["acme"],
        "teams": ["acme/platform"],
        "repos": ["acme/site"],
        "dependency_paths": ["deploy/requirements.yml"],
        "ref_kinds": ["heads"],
        "ref_patterns": ["release/*"],
    }

    # A single source renders as a vertical detail view under the default
    # config — not a boxed one-row table.
    result = runner.invoke(app, ["source", "show", "prod", "--format", "table"])
    assert result.exit_code == 0, result.output
    assert "name: prod" in result.stdout
    assert not any(ch in result.stdout for ch in "╭╮╰╯┌┐└┘│─")

    result = runner.invoke(app, ["source", "remove", "prod"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text()).get("ansible", {}).get("sources") is None


def test_source_list_table_honours_global_collection_view_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
        top_level_ui={"collection_view": "list"},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "list", "--format", "table"])

    assert result.exit_code == 0, result.output
    assert "name: prod" in result.stdout
    assert "repos: acme/site" in result.stdout
    assert "╭" not in result.stdout
    assert "┌" not in result.stdout


def test_source_list_raw_ignores_invalid_global_theme(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
        top_level_ui={"theme": "missing"},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "list", "--format", "raw"])

    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert result.stdout.splitlines() == ["prod"]


def test_alias_list_empty_table_guides_with_stderr_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["alias", "list"])

    assert result.exit_code == 0, result.output
    # Empty human output stays off stdout; the guiding hint goes to stderr.
    assert result.stdout == ""
    assert "No dependency aliases configured" in result.stderr


def test_source_list_empty_table_guides_with_stderr_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "list"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "No sources configured" in result.stderr


def test_source_status_empty_table_guides_with_stderr_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "status"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "No sources scanned yet" in result.stderr


def test_source_list_empty_json_stays_pipe_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(app, ["source", "list", "--format", "json"])

    assert result.exit_code == 0, result.output
    # Structured formats are the pipe targets: empty is a valid [] document and
    # must not carry the human hint on either stream.
    assert result.stdout.strip() == "[]"
    assert "No sources configured" not in result.stderr


def test_source_edit_add_remove_and_clear_updates_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={
            "sources": [
                {
                    "name": "prod",
                    "orgs": ["acme"],
                    "teams": ["acme/old-platform"],
                    "repos": ["acme/site"],
                    "dependency_paths": ["old/requirements.yml"],
                    "ref_kinds": ["heads"],
                    "ref_patterns": ["release/*"],
                }
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "source",
            "edit",
            "prod",
            "--remove-team",
            "acme/old-platform",
            "--add-team",
            "acme/platform",
            "--remove-repo",
            "acme/site",
            "--add-repo",
            "acme/api",
            "--clear-path",
            "--add-path",
            "roles/requirements.yml",
            "--remove-ref-kind",
            "heads",
            "--add-ref-kind",
            "tags",
            "--clear-ref-pattern",
            "--add-ref-pattern",
            "v*",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "updated source 'prod':" in result.stderr
    assert "removed team acme/old-platform" in result.stderr
    assert "added team acme/platform" in result.stderr
    assert "cleared path" in result.stderr
    assert yaml.safe_load(cfg.read_text())["ansible"]["sources"] == [
        {
            "name": "prod",
            "orgs": ["acme"],
            "teams": ["acme/platform"],
            "repos": ["acme/api"],
            "dependency_paths": ["roles/requirements.yml"],
            "ref_kinds": ["tags"],
            "ref_patterns": ["v*"],
        }
    ]


def test_source_edit_clear_boundary_requires_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "teams": ["acme/platform"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(app, ["source", "edit", "prod", "--clear-team"])

    assert result.exit_code == 1
    assert "source requires --org, --team, or --repo" in result.output
    assert yaml.safe_load(cfg.read_text())["ansible"]["sources"] == [
        {"name": "prod", "teams": ["acme/platform"]}
    ]


def test_source_edit_bare_team_removal_uses_original_source_org(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={
            "sources": [
                {
                    "name": "prod",
                    "orgs": ["acme"],
                    "teams": ["acme/platform"],
                }
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "source",
            "edit",
            "prod",
            "--remove-org",
            "acme",
            "--remove-team",
            "platform",
            "--add-repo",
            "acme/site",
        ],
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text())["ansible"]["sources"] == [
        {
            "name": "prod",
            "orgs": [],
            "teams": [],
            "repos": ["acme/site"],
            "dependency_paths": [],
            "ref_kinds": [],
            "ref_patterns": [],
        }
    ]


def test_source_edit_errors_for_unknown_source_missing_mutation_and_missing_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

    cases = [
        (["source", "edit", "missing", "--add-repo", "acme/api"], "unknown source: 'missing'"),
        (["source", "edit", "prod"], "source edit requires at least one mutation flag"),
        (
            ["source", "edit", "prod", "--remove-team", "acme/platform"],
            "source 'prod' has no team acme/platform",
        ),
    ]

    for args, message in cases:
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert message in result.output


def test_source_edit_noop_preserves_cached_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(app, ["source", "edit", "platform", "--add-repo", "acme/site"])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "source 'platform' unchanged" in result.stderr

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )
    assert result.exit_code == 0, result.output
    assert "acme/site" in result.output


def test_source_edit_real_change_clears_cached_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(app, ["source", "edit", "platform", "--add-repo", "acme/api"])
    assert result.exit_code == 0, result.output

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )
    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output


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
        result = CliInvoker().invoke(
            app,
            ["graph", "acme/site", "--downstream", "--depth", "1"],
        )

    assert result.exit_code == 0, result.output
    assert "acme/site" in result.stdout
    assert "|   +-- acme/site@main" in result.stdout
    assert "|       +-- acme/base" in result.stdout
    assert "upstream omitted" not in result.stdout


def test_graph_tree_ignores_global_collection_view_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
        top_level_ui={"collection_view": "list"},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    get_settings.cache_clear()

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/site",
            "--source",
            "platform",
            "--downstream",
            "--cached",
            "--format",
            "tree",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "+-- downstream" in result.stdout
    assert "|   +-- acme/site@main" in result.stdout
    assert "|       +-- acme/base" in result.stdout
    assert "source_repo:" not in result.stdout


def test_graph_inline_upstream_refreshes_and_renders_impact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del source, aliases, settings, concurrency
        _seed_index(
            index,
            source_key,
            (
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
        return RefreshResult(source_key=source_key, repos=1, refs=1, edges=1, changed_refs=1)

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
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
    assert "    +-- acme/base@v1" in result.stdout
    assert "        +-- acme/site@main" in result.stdout
    assert SqliteDependencyIndex(index_path).dependents("acme/base", "v1", source_key=None)


def test_graph_inline_source_reuses_fingerprint_cache_without_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()
    refresh_calls = 0

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        nonlocal refresh_calls
        del source, aliases, settings, concurrency
        refresh_calls += 1
        _seed_index(
            index,
            source_key,
            (
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
        return RefreshResult(source_key=source_key, repos=1, refs=1, edges=1, changed_refs=1)

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

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

    second = runner.invoke(
        app,
        [
            "graph",
            "acme/base",
            "--org",
            "acme",
            "--ref-kind",
            "heads",
            "--upstream",
            "--cached",
        ],
    )

    assert second.exit_code == 0, second.output
    assert "    +-- acme/site@main" in second.stdout
    assert refresh_calls == 1


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

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )

    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output
    assert "untaped-ansible source refresh platform" in result.output
    assert "untaped-ansible graph acme/base --source platform --upstream --refresh" in result.output


def test_graph_repeated_sources_union_cached_upstream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    index = SqliteDependencyIndex(index_path)
    scanned_at = datetime.now(UTC)
    _seed_index(
        index,
        "source:platform",
        (
            IndexedDependency(
                source_repo="acme/site",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
        scanned_at=scanned_at,
    )
    _seed_index(
        index,
        "source:ops",
        (
            IndexedDependency(
                source_repo="acme/deploy",
                source_ref="release",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
        scanned_at=scanned_at,
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [
                {"name": "platform", "repos": ["acme/site"]},
                {"name": "ops", "repos": ["acme/deploy"]},
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--source",
            "platform",
            "--source",
            "ops",
            "--upstream",
            "--cached",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout
    assert "    +-- acme/deploy@release" in result.stdout


def test_graph_repeated_sources_refresh_each_saved_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [
                {"name": "platform", "repos": ["acme/site"]},
                {"name": "ops", "repos": ["acme/deploy"]},
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    calls: list[tuple[str, str]] = []

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del aliases, settings, concurrency
        calls.append((source.name, source_key))
        source_repo = source.repos[0]
        source_ref = "main" if source.name == "platform" else "release"
        _seed_index(
            index,
            source_key,
            (
                IndexedDependency(
                    source_repo=source_repo,
                    source_ref=source_ref,
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
        return RefreshResult(
            source_key=source_key,
            repos=1,
            refs=1,
            edges=1,
            changed_refs=1,
            unchanged_refs=0,
        )

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--source",
            "platform",
            "--source",
            "ops",
            "--upstream",
            "--refresh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("platform", "source:platform"), ("ops", "source:ops")]
    assert "    +-- acme/site@main" in result.stdout
    assert "    +-- acme/deploy@release" in result.stdout


def test_graph_repeated_sources_cached_reports_missing_named_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:ops",
        (
            IndexedDependency(
                source_repo="acme/deploy",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [
                {"name": "platform", "repos": ["acme/site"]},
                {"name": "ops", "repos": ["acme/deploy"]},
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--source",
            "platform",
            "--source",
            "ops",
            "--upstream",
            "--cached",
        ],
    )

    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output


def test_graph_repeated_sources_downstream_cached_reports_missing_named_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:ops",
        (
            IndexedDependency(
                source_repo="acme/deploy",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [
                {"name": "platform", "repos": ["acme/site"]},
                {"name": "ops", "repos": ["acme/deploy"]},
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/deploy",
            "--source",
            "platform",
            "--source",
            "ops",
            "--downstream",
            "--cached",
        ],
    )

    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output


def test_graph_repeated_sources_both_cached_reports_missing_named_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:ops",
        (
            IndexedDependency(
                source_repo="acme/deploy",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [
                {"name": "platform", "repos": ["acme/site"]},
                {"name": "ops", "repos": ["acme/deploy"]},
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--source",
            "platform",
            "--source",
            "ops",
            "--cached",
        ],
    )

    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output


def test_source_save_clears_cached_data_for_redefined_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/old-site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

    result = runner.invoke(app, ["source", "save", "platform", "--repo", "acme/new-site"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )
    assert result.exit_code == 1
    assert "no cached source data found for source 'platform'" in result.output


def test_source_save_preserves_cached_data_for_identical_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

    result = runner.invoke(app, ["source", "save", "platform", "--repo", "acme/site"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )
    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout


def test_graph_cached_missing_ref_lists_available_refs_in_display_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    now = datetime(2026, 6, 1, tzinfo=UTC)
    scans = tuple(
        RefScan(
            source_key="source:platform",
            source_repo="acme/site",
            ref_kind=ref_kind,
            source_ref=source_ref,
            source_sha=f"sha-{source_ref}",
            clone_url="https://github.com/acme/site.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=now,
            indexed_at=now,
            dependencies=(),
        )
        for ref_kind, source_ref in (
            ("tags", "v1.0.0"),
            ("heads", "trunk"),
            ("tags", "v2.0.0"),
            ("heads", "feature/2"),
            ("heads", "docs"),
        )
    )
    SqliteDependencyIndex(index_path).commit_source_ref_refresh(
        "source:platform",
        scans=scans,
        touches=(),
        keep={(scan.source_repo, scan.ref_kind, scan.source_ref) for scan in scans},
        repo_metadata=(
            SourceRepoMetadata(
                source_key="source:platform",
                source_repo="acme/site",
                default_branch="trunk",
            ),
        ),
        scanned_at=now,
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/site",
            "--ref",
            "missing",
            "--source",
            "platform",
            "--downstream",
            "--cached",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ("available refs: trunk, docs, feature/2, v2.0.0, v1.0.0") in result.stdout
    assert "Run `untaped-ansible source refresh platform` to update it." in result.stdout


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
        result = CliInvoker().invoke(app, ["graph", "acme/site", "--both", "--depth", "1"])

    assert result.exit_code == 0, result.output
    assert "|   +-- acme/site@main" in result.stdout
    assert "|       +-- acme/base" in result.stdout
    assert (
        "warning: only showing downstream; upstream omitted because no source is configured"
        in result.stdout
    )


def test_graph_downstream_with_source_uses_cached_data_without_live_reads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        ansible_profile={"stale_after": 60},
        top_level_ansible={
            "sources": [{"name": "platform", "repos": ["acme/site"]}],
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        result = CliInvoker().invoke(
            app,
            [
                "graph",
                "acme/site",
                "--source",
                "platform",
                "--downstream",
                "--depth",
                "1",
                "--cached",
            ],
        )
        assert len(mock.calls) == 0

    assert result.exit_code == 0, result.output
    assert "|   +-- acme/site@main" in result.stdout
    assert "|       +-- acme/cached" in result.stdout


def test_graph_downstream_with_source_live_flag_reads_remote_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
        result = CliInvoker().invoke(
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
    assert "|   +-- acme/site@main" in result.stdout
    assert "|       +-- acme/live" in result.stdout
    assert "acme/cached" not in result.stdout


@pytest.mark.parametrize(
    "args",
    [["--upstream", "--downstream"], ["--upstream", "--both"]],
)
def test_graph_direction_flags_are_mutually_exclusive_at_parse_time(
    tmp_path: Path,
    monkeypatch,
    args: list[str],
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(app, ["graph", "acme/site", *args])
    output = " ".join(result.output.split())

    assert result.exit_code == 2, result.output
    assert "Mutually exclusive arguments" in output
    for flag in args:
        assert flag in output


@pytest.mark.parametrize(
    "args",
    [["--refresh", "--cached"], ["--cached", "--live"], ["--refresh", "--live"]],
)
def test_graph_refresh_cached_and_live_are_mutually_exclusive_at_parse_time(
    tmp_path: Path,
    monkeypatch,
    args: list[str],
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "platform", "orgs": ["acme"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(app, ["graph", "acme/site", "--source", "platform", *args])
    output = " ".join(result.output.split())

    assert result.exit_code == 2, result.output
    assert "Mutually exclusive arguments" in output
    for flag in args:
        assert flag in output


def test_graph_refresh_without_source_fails_fast_with_usage_error() -> None:
    result = CliInvoker().invoke(app, ["graph", "acme/site", "--refresh"])

    assert result.exit_code == 2, result.output
    assert "--refresh requires --source or inline source selectors" in result.output


def test_graph_refresh_accepts_ref_scan_default_as_inline_selector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/site", "--refresh", "--ref-scan-default", "default_branch"],
    )

    assert result.exit_code == 1
    assert "source requires --org, --team, or --repo" in result.output


def test_graph_source_conflicts_with_inline_selectors(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "platform", "orgs": ["acme"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/site", "--source", "platform", "--org", "acme"],
    )
    output = " ".join(result.output.replace("│", " ").split())

    assert result.exit_code == 2
    assert "--source cannot be combined with --org, --team, --repo, --path" in output


def test_graph_source_conflicts_with_ref_scan_default_selector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        top_level_ansible={"sources": [{"name": "platform", "orgs": ["acme"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/site",
            "--source",
            "platform",
            "--ref-scan-default",
            "default_branch",
        ],
    )
    output = " ".join(result.output.replace("│", " ").split())

    assert result.exit_code == 2
    assert "--source cannot be combined with" in output
    assert "--ref-scan-default" in output


def test_source_save_validates_search_boundary_repo_and_ref_kind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

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

    result = CliInvoker().invoke(app, ["source", "refresh", "bad"])

    assert result.exit_code == 1
    assert "repo must be owner/name" in result.output


def test_inline_source_cache_key_is_order_insensitive(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()
    refresh_calls = 0

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        nonlocal refresh_calls
        del aliases, settings, concurrency
        refresh_calls += 1
        _seed_index(
            index,
            source_key,
            tuple(
                IndexedDependency(
                    source_repo=repo,
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                )
                for repo in source.repos
            ),
        )
        return RefreshResult(
            source_key=source_key,
            repos=len(source.repos),
            refs=len(source.repos),
            edges=len(source.repos),
            changed_refs=len(source.repos),
        )

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

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
            "--cached",
        ],
    )

    assert second.exit_code == 0, second.output
    assert "acme/a@main" in second.stdout
    assert "acme/b@main" in second.stdout
    assert refresh_calls == 1


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

    result = CliInvoker().invoke(
        app,
        ["graph", str(target), "--target-repo", "acme/base", "--downstream"],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("acme/base\n")
    assert "|   +-- acme/base" in result.stdout
    assert "|       +-- acme/users" in result.stdout


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

    result = CliInvoker().invoke(app, ["graph", str(target), "--downstream"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("acme/worktree-role\n")
    assert "|   +-- acme/worktree-role" in result.stdout
    assert "|       +-- acme/users" in result.stdout


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

    result = CliInvoker().invoke(app, ["graph", str(target), "--downstream"])

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

    result = CliInvoker().invoke(
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
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
            IndexedDependency(
                source_repo="acme/empty",
                source_ref="main",
                dependency_repo="acme/stale",
                dependency_name="stale",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
    )
    target = tmp_path / "role"
    target.mkdir()
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
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
        result = CliInvoker().invoke(
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
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={
            "sources": [{"name": "platform", "repos": ["acme/site"]}],
            "aliases": {"base": "acme/base"},
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        ["graph", "base", "--source", "platform", "--upstream", "--cached"],
    )

    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout


def test_graph_help_teaches_clean_source_first_workflow() -> None:
    result = CliInvoker().invoke(app, ["graph", "--help"])
    output = " ".join(result.output.replace("│", " ").split())

    assert result.exit_code == 0, result.output
    assert "--upstream" in output
    assert "--downstream" in output
    assert "--source" in output
    assert "--refresh" in output
    assert "--cached" in output
    assert "--kind" not in output
    assert "--cache-backend" not in output
    assert "--concurrency" in output
    assert "--live" in output
    assert "--target-repo" in output
    assert "--both" in output
    assert "Show upstream and downstream (default)." in output
    assert "Show what TARGET depends on (works without a source)." in output
    assert "Show repos that depend on TARGET (reverse impact; requires a source)." in output
    assert "deterministic fingerprint key" in output
    assert "Examples:" in output
    assert (
        "untaped-ansible graph acme/base --org acme --team platform --upstream --refresh" in output
    )
    assert "--scope" not in output
    assert "--direction" not in output


def test_graph_bare_invocation_requires_target() -> None:
    result = CliInvoker().invoke(app, ["graph"])

    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "requires an argument" in result.stderr
    assert "TARGET" in result.stderr


def test_source_edit_help_does_not_expose_negative_clear_aliases() -> None:
    result = CliInvoker().invoke(app, ["source", "edit", "--help"])

    assert result.exit_code == 0, result.output
    assert "--clear-org" in result.output
    assert "--no-clear-org" not in result.output
    assert "--no-clear-team" not in result.output
    assert "--no-clear-repo" not in result.output


def test_source_status_classifies_missing_unindexed_and_stale_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:stale",
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"stale_after": 60},
        top_level_ansible={
            "sources": [
                {"name": "unindexed", "repos": ["acme/site"]},
                {"name": "stale", "repos": ["acme/base"]},
            ],
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    runner = CliInvoker()

    result = runner.invoke(app, ["source", "status", "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = {row["source"]: row for row in json.loads(result.stdout)}
    assert rows["unindexed"]["state"] == "not-refreshed"
    assert rows["stale"]["state"] == "stale"

    result = runner.invoke(app, ["source", "status", "missing", "--format", "json"])
    assert result.exit_code == 1
    assert "unknown source: 'missing'" in result.output


def test_source_refresh_scans_source_with_git_backend(tmp_path: Path, monkeypatch) -> None:
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

    class FakeGitRefresh:
        def __init__(self, **kwargs) -> None:
            assert kwargs["aliases"] == {"common": "acme/common"}
            self._index = kwargs["index"]

        def __call__(self, source, *, source_key: str) -> RefreshResult:
            assert source.repos == ["acme/site"]
            _seed_index(
                self._index,
                source_key,
                (
                    IndexedDependency(
                        source_repo="acme/site",
                        source_ref="main",
                        dependency_repo="acme/common",
                        dependency_name="common",
                        dependency_version=None,
                        source_path="roles/requirements.yml",
                    ),
                ),
                scanned_at=datetime.now(UTC),
            )
            return RefreshResult(
                source_key=source_key,
                repos=1,
                refs=1,
                edges=1,
                changed_refs=1,
                unchanged_refs=0,
            )

    monkeypatch.setattr(_refresh, "RefreshGitSourceIndex", FakeGitRefresh)

    result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 0, result.output
    assert "refreshed source 'prod': 1 repos, 1 refs, 1 edges" in result.stderr
    assert SqliteDependencyIndex(index_path).dependents(
        "acme/common", None, source_key="source:prod"
    )


def test_source_refresh_survives_invalid_ui_theme(tmp_path: Path, monkeypatch) -> None:
    # The refresh progress UiContext is built strict=False, so a misconfigured
    # theme degrades the spinner to the default theme instead of failing an
    # otherwise-valid refresh on the data path.
    cfg = _write_config(
        tmp_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
        top_level_ui={"theme": "missing"},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    class FakeGitRefresh:
        def __init__(self, **kwargs) -> None:
            self._index = kwargs["index"]

        def __call__(self, source, *, source_key: str) -> RefreshResult:
            return RefreshResult(
                source_key=source_key,
                repos=1,
                refs=1,
                edges=1,
                changed_refs=1,
                unchanged_refs=0,
            )

    monkeypatch.setattr(_refresh, "RefreshGitSourceIndex", FakeGitRefresh)

    result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 0, result.output
    assert "refreshed source 'prod':" in result.stderr


def test_source_refresh_uses_basic_auth_for_git_backend(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    captured: dict[str, str | None] = {}

    class FakeGitRefresh:
        def __init__(self, **kwargs) -> None:
            captured["auth_header"] = kwargs["auth_header"]
            captured["concurrency"] = kwargs["concurrency"]

        def __call__(self, source, *, source_key: str) -> RefreshResult:
            return RefreshResult(
                source_key=source_key,
                repos=1,
                refs=1,
                edges=0,
                changed_refs=1,
                unchanged_refs=0,
            )

    monkeypatch.setattr(_refresh, "RefreshGitSourceIndex", FakeGitRefresh)

    result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 0, result.output
    credential = b64encode(b"x-access-token:ghp_test").decode()
    assert captured["auth_header"] == f"AUTHORIZATION: basic {credential}"
    assert captured["concurrency"] == 8


def test_source_refresh_allows_git_concurrency_override(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(
        tmp_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    captured: dict[str, int] = {}

    class FakeGitRefresh:
        def __init__(self, **kwargs) -> None:
            captured["concurrency"] = kwargs["concurrency"]

        def __call__(self, source, *, source_key: str) -> RefreshResult:
            return RefreshResult(
                source_key=source_key,
                repos=1,
                refs=1,
                edges=0,
                changed_refs=1,
                unchanged_refs=0,
            )

    monkeypatch.setattr(_refresh, "RefreshGitSourceIndex", FakeGitRefresh)

    result = CliInvoker().invoke(app, ["source", "refresh", "prod", "--concurrency", "5"])

    assert result.exit_code == 0, result.output
    assert captured["concurrency"] == 5
    assert "1 changed, 0 unchanged" in result.stderr
    assert "concurrency 5" in result.stderr


def test_graph_with_source_uses_cache_by_default_with_git_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    def fail_refresh(*args, **kwargs) -> RefreshResult:
        raise AssertionError("graph must not refresh source data unless --refresh is passed")

    monkeypatch.setattr(_refresh, "refresh_source", fail_refresh)

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--concurrency", "4"],
    )

    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout
    assert "changed" not in result.stderr


def test_graph_with_source_missing_cache_fails_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        index_path=tmp_path / "index.sqlite3",
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--both"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "no cached source data found for source 'platform'" in result.stderr
    assert "untaped-ansible source refresh platform" in result.stderr


def test_graph_inline_upstream_with_ref_renders_all_matching_source_refs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    captured: dict[str, object] = {}

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del aliases, settings, concurrency
        captured["ref_kinds"] = source.ref_kinds
        captured["ref_patterns"] = source.ref_patterns
        _seed_index(
            index,
            source_key,
            (
                IndexedDependency(
                    source_repo="acme/playbook",
                    source_ref="master",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v3",
                    source_path="roles/requirements.yml",
                ),
                IndexedDependency(
                    source_repo="acme/playbook",
                    source_ref="v3",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v3",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
        return RefreshResult(source_key=source_key, repos=1, refs=2, edges=2)

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--ref",
            "v3",
            "--org",
            "acme",
            "--team",
            "platform",
            "--upstream",
            "--refresh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"ref_kinds": [], "ref_patterns": []}
    assert "    +-- acme/playbook@master" in result.stdout
    assert "    +-- acme/playbook@v3" in result.stdout


def test_graph_inline_source_preserves_repeated_selectors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    captured: dict[str, object] = {}

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del aliases, settings, concurrency
        captured["orgs"] = source.orgs
        captured["teams"] = source.teams
        captured["repos"] = source.repos
        captured["paths"] = source.dependency_paths
        captured["ref_kinds"] = source.ref_kinds
        captured["ref_patterns"] = source.ref_patterns
        _seed_index(index, source_key)
        return RefreshResult(source_key=source_key, repos=0, refs=0, edges=0)

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--org",
            "acme",
            "--org",
            "beta",
            "--team",
            "acme/platform",
            "--team",
            "beta/platform",
            "--repo",
            "acme/site",
            "--repo",
            "beta/site",
            "--path",
            "roles/requirements.yml",
            "--path",
            "meta/main.yml",
            "--ref-kind",
            "heads",
            "--ref-kind",
            "tags",
            "--ref-pattern",
            "main",
            "--ref-pattern",
            "v*",
            "--upstream",
            "--refresh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "orgs": ["acme", "beta"],
        "teams": ["acme/platform", "beta/platform"],
        "repos": ["acme/site", "beta/site"],
        "paths": ["meta/main.yml", "roles/requirements.yml"],
        "ref_kinds": ["heads", "tags"],
        "ref_patterns": ["main", "v*"],
    }


def test_graph_inline_source_passes_ref_scan_default_to_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(tmp_path, index_path=index_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    captured: dict[str, object] = {}

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del aliases, settings, github_settings, http, concurrency, on_progress
        captured["ref_scan_default"] = source.ref_scan_default
        _seed_index(index, source_key)
        return RefreshResult(source_key=source_key, repos=0, refs=0, edges=0)

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
        app,
        [
            "graph",
            "acme/base",
            "--repo",
            "acme/site",
            "--ref-scan-default",
            "default_branch",
            "--upstream",
            "--refresh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"ref_scan_default": "default_branch"}


def test_graph_cached_skips_source_refresh(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (
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
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    def fail_refresh(*args, **kwargs) -> RefreshResult:
        raise AssertionError("--cached must not refresh source data")

    monkeypatch.setattr(_refresh, "refresh_source", fail_refresh)

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )

    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout


def _fresh_platform_dependency() -> IndexedDependency:
    return IndexedDependency(
        source_repo="acme/site",
        source_ref="main",
        dependency_repo="acme/base",
        dependency_name="base",
        dependency_version=None,
        source_path="roles/requirements.yml",
    )


def _counting_refresh(calls: list[str]):
    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del aliases, settings, concurrency
        calls.append(source.name)
        source_repo = source.repos[0]
        _seed_index(
            index,
            source_key,
            (
                IndexedDependency(
                    source_repo=source_repo,
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
        return RefreshResult(source_key=source_key, repos=1, refs=1, edges=1, changed_refs=1)

    return fake_refresh


def test_graph_cache_first_ignores_freshness_ttl_for_fresh_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime.now(UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"freshness_ttl": 3600},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        result = CliInvoker().invoke(
            app,
            ["graph", "acme/base", "--source", "platform", "--upstream"],
        )
        assert len(mock.calls) == 0

    assert result.exit_code == 0, result.output
    assert "    +-- acme/site@main" in result.stdout
    assert result.stderr == ""


def test_graph_cache_first_does_not_print_freshness_ttl_skip_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime.now(UTC) - timedelta(hours=3),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"freshness_ttl": 14400},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        result = CliInvoker().invoke(
            app,
            ["graph", "acme/base", "--source", "platform", "--upstream"],
        )
        assert len(mock.calls) == 0

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_graph_cache_first_missing_source_fails_even_with_freshness_ttl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"freshness_ttl": 3600},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    calls: list[str] = []
    monkeypatch.setattr(_refresh, "refresh_source", _counting_refresh(calls))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream"],
    )

    assert result.exit_code == 1
    assert calls == []
    assert "no cached source data found for source 'platform'" in result.stderr


def test_graph_freshness_ttl_with_refresh_flag_still_probes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime.now(UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"freshness_ttl": 3600},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    calls: list[str] = []
    monkeypatch.setattr(_refresh, "refresh_source", _counting_refresh(calls))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--refresh"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["platform"]
    assert "skipping check" not in result.stderr


def test_graph_cache_first_uses_stale_source_without_freshness_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"freshness_ttl": 60},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    calls: list[str] = []
    monkeypatch.setattr(_refresh, "refresh_source", _counting_refresh(calls))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream"],
    )

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "    +-- acme/site@main" in result.stdout
    assert result.stderr == ""


def test_graph_cache_first_ignores_freshness_ttl_for_mixed_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    index = SqliteDependencyIndex(index_path)
    _seed_index(
        index,
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime.now(UTC),
    )
    _seed_index(
        index,
        "source:ops",
        (
            IndexedDependency(
                source_repo="acme/deploy",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version=None,
                source_path="roles/requirements.yml",
            ),
        ),
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"freshness_ttl": 3600},
        top_level_ansible={
            "sources": [
                {"name": "platform", "repos": ["acme/site"]},
                {"name": "ops", "repos": ["acme/deploy"]},
            ]
        },
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    calls: list[str] = []
    monkeypatch.setattr(_refresh, "refresh_source", _counting_refresh(calls))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--source", "ops", "--upstream"],
    )

    assert result.exit_code == 0, result.output
    assert calls == []
    assert result.stderr == ""
    assert "    +-- acme/site@main" in result.stdout
    assert "    +-- acme/deploy@main" in result.stdout


def test_graph_stale_warning_includes_exact_refresh_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        ansible_profile={"stale_after": 60},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--cached"],
    )

    assert result.exit_code == 0, result.output
    assert "source data is stale" in result.stdout
    assert "Run `untaped-ansible source refresh platform` to update it." in result.stdout


def test_source_save_expands_bare_team_slug_with_single_org(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        ["source", "save", "prod", "--org", "acme", "--team", "platform"],
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text())["ansible"]["sources"][0]["teams"] == ["acme/platform"]


def test_source_save_records_ref_scan_default(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    result = CliInvoker().invoke(
        app,
        [
            "source",
            "save",
            "prod",
            "--repo",
            "acme/site",
            "--ref-scan-default",
            "default_branch",
        ],
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(cfg.read_text())["ansible"]["sources"][0]["ref_scan_default"] == (
        "default_branch"
    )


def test_source_refresh_partial_failure_exits_nonzero_and_saves_successes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/gone", "acme/ok"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    _seed_unchanged_scan(monkeypatch, {"acme/ok": "sha-ok"}, missing=("acme/gone",))

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_repos(mock, {"acme/ok": "sha-ok"}, missing=("acme/gone",))
        result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "refreshed source 'prod':" in result.stderr
    assert "failed acme/gone: " in result.stderr
    assert "refresh completed with 1 repo failure; successes were saved" in result.output
    # the succeeded repo's cached scan survived the partial failure
    assert SqliteDependencyIndex(index_path).ref_scans(
        "source:prod", "acme/ok", [("heads", "main")]
    )


def test_source_refresh_budget_pause_exits_nonzero_without_repo_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        index_path=tmp_path / "index.sqlite3",
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/a", "acme/b"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del source, index, aliases, settings, github_settings, http, concurrency, on_progress
        return RefreshResult(
            source_key=source_key,
            completed=False,
            pause_reason="GitHub GraphQL rate limit is low: 200 points remaining",
            repos=2,
            refs=1,
            edges=1,
            changed_refs=1,
            rate_limit_remaining=200,
        )

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "failed acme/" not in result.stderr
    assert "GitHub GraphQL rate limit is low: 200 points remaining" in result.stderr
    assert "resume with `untaped-ansible source refresh prod`" in result.stderr


def test_source_refresh_all_failures_exits_nonzero_and_leaves_index_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/gone", "acme/ok"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    _seed_unchanged_scan(monkeypatch, {"acme/ok": "sha-ok"}, missing=("acme/gone",))
    before = SqliteDependencyIndex(index_path).status("source:prod")
    assert before is not None

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_repos(mock, {}, missing=("acme/gone", "acme/ok"))
        result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 1
    assert "failed acme/gone: " in result.stderr
    assert "failed acme/ok: " in result.stderr
    assert "refresh failed for all 2 repos; index left unchanged" in result.output
    # the index commit was skipped: cached data and freshness are untouched
    after = SqliteDependencyIndex(index_path).status("source:prod")
    assert after is not None
    assert after.scanned_at == before.scanned_at
    assert SqliteDependencyIndex(index_path).ref_scans(
        "source:prod", "acme/ok", [("heads", "main")]
    )


def test_source_refresh_global_graphql_error_exits_once_without_per_repo_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _write_config(
        tmp_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/gone", "acme/ok"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_graphql_error(
            mock,
            ("acme/gone", "acme/ok"),
            httpx.Response(403, json={"message": "API rate limit exceeded for user ID 123."}),
        )
        result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.count("github graphql rate limit exceeded") == 1
    assert "API rate limit exceeded" in result.stderr
    assert "failed acme/gone:" not in result.stderr
    assert "failed acme/ok:" not in result.stderr
    assert "refresh failed for all" not in result.stderr


def test_source_refresh_emits_progress_lines_on_stderr_in_non_tty_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/ok"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    _seed_unchanged_scan(monkeypatch, {"acme/ok": "sha-ok"})

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_repos(mock, {"acme/ok": "sha-ok"})
        result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "probing refs: 1/1 repos" in result.stderr
    assert "fetching changes: 1/1 repos, 0 changed" in result.stderr


def test_source_refresh_warns_when_graphql_rate_limit_is_low(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "prod", "repos": ["acme/ok"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))
    _seed_unchanged_scan(monkeypatch, {"acme/ok": "sha-ok"})

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_repos(mock, {"acme/ok": "sha-ok"}, rate_limit_remaining=200)
        result = CliInvoker().invoke(app, ["source", "refresh", "prod"])

    assert result.exit_code == 0, result.output
    assert "warning: GitHub GraphQL rate limit is low: 200 points remaining" in result.stderr


def test_graph_refresh_with_partial_failures_warns_and_proceeds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del source, aliases, settings, concurrency
        _seed_index(
            index,
            source_key,
            (
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
        return RefreshResult(
            source_key=source_key,
            repos=2,
            refs=1,
            edges=1,
            changed_refs=1,
            failures=(RepoFailure(repo="acme/gone", reason="boom"),),
        )

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--refresh"],
    )

    assert result.exit_code == 0, result.output
    assert (
        "warning: refresh of platform had 1 failure; data for those repos may be stale"
        in result.stdout
    )
    assert "    +-- acme/site@main" in result.stdout


def test_graph_refresh_budget_pause_exits_without_rendering_stale_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    def fake_refresh(
        source,
        *,
        source_key: str,
        index: SqliteDependencyIndex,
        aliases: dict[str, str],
        settings,
        github_settings,
        http,
        concurrency: int,
        on_progress=None,
    ) -> RefreshResult:
        del source, index, aliases, settings, github_settings, http, concurrency, on_progress
        return RefreshResult(
            source_key=source_key,
            completed=False,
            pause_reason="GitHub GraphQL rate limit is low: 200 points remaining",
            repos=2,
            refs=1,
            edges=1,
            changed_refs=1,
            rate_limit_remaining=200,
        )

    monkeypatch.setattr(_refresh, "refresh_source", fake_refresh)

    result = CliInvoker().invoke(
        app,
        ["graph", "acme/base", "--source", "platform", "--upstream", "--refresh"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "GitHub GraphQL rate limit is low: 200 points remaining" in result.stderr
    assert "acme/site@main" not in result.stdout


def test_graph_refresh_global_graphql_error_exits_without_rendering_stale_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index_path = tmp_path / "index.sqlite3"
    _seed_index(
        SqliteDependencyIndex(index_path),
        "source:platform",
        (_fresh_platform_dependency(),),
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cfg = _write_config(
        tmp_path,
        index_path=index_path,
        extra_profile={"github": {"token": "ghp_test"}},
        top_level_ansible={"sources": [{"name": "platform", "repos": ["acme/site"]}]},
    )
    monkeypatch.setenv("UNTAPED_CONFIG", str(cfg))

    with respx.mock(base_url="https://api.github.com") as mock:
        _mock_refresh_graphql_error(
            mock,
            ("acme/site",),
            httpx.Response(403, json={"message": "API rate limit exceeded for user ID 123."}),
        )
        result = CliInvoker().invoke(
            app,
            ["graph", "acme/base", "--source", "platform", "--upstream", "--refresh"],
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.count("github graphql rate limit exceeded") == 1
    assert "API rate limit exceeded" in result.stderr
    assert "warning: refresh of platform" not in result.stdout
    assert "acme/site@main" not in result.stdout


def teardown_module() -> None:
    get_settings.cache_clear()
