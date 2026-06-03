"""Tests for the SQLite dependency index repository."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from untaped_ansible.domain.payloads import (
    CachedRef,
    IndexedDependency,
    RefScan,
    SourceRepoMetadata,
)
from untaped_ansible.infrastructure.sqlite_index import (
    IndexScan,
    SqliteDependencyIndex,
)
from untaped_ansible.infrastructure.sqlite_schema import ensure_column


def test_replace_source_scan_supports_dependency_and_reverse_lookup(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    scanned_at = datetime(2026, 5, 29, tzinfo=UTC)

    index.replace_source_scan(
        IndexScan(
            source_key="source:prod",
            scanned_at=scanned_at,
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
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    source_sha="sha-main",
                    dependency_name="common",
                    dependency_version=None,
                    source_path="meta/main.yml",
                    unresolved="common",
                ),
            ),
        )
    )

    assert index.dependencies("acme/site", "main", source_key="source:prod") == [
        IndexedDependency(
            source_repo="acme/site",
            source_ref="main",
            source_sha="sha-main",
            dependency_repo="acme/base",
            dependency_name="base",
            dependency_version="v1",
            source_path="roles/requirements.yml",
        ),
        IndexedDependency(
            source_repo="acme/site",
            source_ref="main",
            source_sha="sha-main",
            dependency_name="common",
            dependency_version=None,
            source_path="meta/main.yml",
            unresolved="common",
        ),
    ]
    assert index.dependents("acme/base", "v1", source_key="source:prod")[0].source_repo == (
        "acme/site"
    )
    assert index.dependents("acme/base", "v2", source_key="source:prod") == []
    assert [
        edge.dependency_name
        for edge in index.dependencies("acme/site", None, source_key="source:prod")
    ] == [
        "base",
        "common",
    ]


def test_status_staleness_and_clear_source(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    scanned_at = datetime.now(UTC) - timedelta(days=2)
    index.replace_source_scan(
        IndexScan(
            source_key="source:prod",
            scanned_at=scanned_at,
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

    status = index.status("source:prod")

    assert status is not None
    assert status.source_key == "source:prod"
    assert status.edges == 1
    assert status.repos == 1
    assert status.refs == 1
    assert index.is_stale("source:prod", max_age_seconds=60)

    index.clear("source:prod")

    assert index.status("source:prod") is None
    assert not index.is_stale("source:prod", max_age_seconds=60)


def test_status_uses_scan_metadata_when_source_has_no_edges(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    index.replace_source_scan(
        IndexScan(
            source_key="source:prod",
            scanned_at=datetime.now(UTC),
            repos=2,
            refs=5,
            dependencies=(),
        )
    )

    status = index.status("source:prod")

    assert status is not None
    assert status.repos == 2
    assert status.refs == 5
    assert status.edges == 0


def test_replace_ref_scan_tracks_metadata_and_replaces_only_that_ref(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    indexed_at = datetime(2026, 6, 1, 12, tzinfo=UTC)
    checked_at = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)

    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/site",
            ref_kind="heads",
            source_ref="main",
            source_sha="sha-main-1",
            backend="git",
            clone_url="https://github.com/acme/site.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=checked_at,
            indexed_at=indexed_at,
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    source_sha="sha-main-1",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v1",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/site",
            ref_kind="heads",
            source_ref="release",
            source_sha="sha-release",
            backend="git",
            clone_url="https://github.com/acme/site.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=checked_at,
            indexed_at=indexed_at,
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="release",
                    source_sha="sha-release",
                    dependency_repo="acme/release-base",
                    dependency_name="release-base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )

    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/site",
            ref_kind="heads",
            source_ref="main",
            source_sha="sha-main-2",
            backend="git",
            clone_url="https://github.com/acme/site.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=checked_at,
            indexed_at=indexed_at,
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    source_sha="sha-main-2",
                    dependency_repo="acme/new-base",
                    dependency_name="new-base",
                    dependency_version="v2",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )

    metadata = index.ref_scan("source:prod", "acme/site", "heads", "main")

    assert metadata is not None
    assert metadata.source_sha == "sha-main-2"
    assert metadata.backend == "git"
    assert metadata.checked_at == checked_at
    assert metadata.aliases_fingerprint == ""
    assert (
        index.dependencies("acme/site", "main", source_key="source:prod")[0].source_ref_kind
        == "heads"
    )
    dependency_repos = [
        edge.dependency_repo
        for edge in index.dependencies("acme/site", None, source_key="source:prod")
    ]
    assert dependency_repos == [
        "acme/release-base",
        "acme/new-base",
    ]
    assert index.status("source:prod") is None

    index.finalize_source_ref_scan("source:prod", scanned_at=checked_at)
    status = index.status("source:prod")

    assert status is not None
    assert status.repos == 1
    assert status.refs == 2
    assert status.edges == 2


def test_empty_ref_scan_is_retained_and_pruned_by_selected_refs(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    now = datetime(2026, 6, 1, tzinfo=UTC)

    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/empty",
            ref_kind="heads",
            source_ref="main",
            source_sha="sha-empty",
            backend="git",
            clone_url="https://github.com/acme/empty.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=now,
            indexed_at=now,
            dependencies=(),
        )
    )
    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/old",
            ref_kind="heads",
            source_ref="main",
            source_sha="sha-old",
            backend="git",
            clone_url="https://github.com/acme/old.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=now,
            indexed_at=now,
            dependencies=(),
        )
    )

    assert index.status("source:prod") is None

    index.finalize_source_ref_scan("source:prod", scanned_at=now)
    assert index.status("source:prod").refs == 2  # type: ignore[union-attr]

    index.prune_source_refs("source:prod", {("acme/empty", "heads", "main")})
    index.finalize_source_ref_scan("source:prod", scanned_at=now)

    assert index.ref_scan("source:prod", "acme/empty", "heads", "main") is not None
    assert index.ref_scan("source:prod", "acme/old", "heads", "main") is None
    assert index.status("source:prod").refs == 1  # type: ignore[union-attr]


def test_cached_refs_include_ref_scans_and_legacy_source_edges(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    index.replace_source_scan(
        IndexScan(
            source_key="source:prod",
            scanned_at=now,
            dependencies=(
                IndexedDependency(
                    source_repo="acme/legacy",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/site",
            ref_kind="heads",
            source_ref="release/1",
            source_sha="sha-release",
            backend="git",
            clone_url="https://github.com/acme/site.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=now,
            indexed_at=now,
            dependencies=(),
        )
    )

    assert index.cached_refs("acme/site", source_key="source:prod") == {"release/1"}
    assert index.cached_refs("acme/legacy", source_key="source:prod") == {"main"}
    assert index.cached_refs("acme/site", source_key=None) == set()


def test_cached_ref_metadata_includes_ref_kind_and_default_branch(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    now = datetime(2026, 6, 1, tzinfo=UTC)

    index.commit_source_ref_refresh(
        "source:prod",
        scans=(
            RefScan(
                source_key="source:prod",
                source_repo="acme/site",
                ref_kind="tags",
                source_ref="v2.0.0",
                source_sha="sha-v2",
                backend="git",
                clone_url="https://github.com/acme/site.git",
                clone_protocol="https",
                dependency_paths_fingerprint="paths-a",
                checked_at=now,
                indexed_at=now,
                dependencies=(),
            ),
            RefScan(
                source_key="source:prod",
                source_repo="acme/site",
                ref_kind="heads",
                source_ref="trunk",
                source_sha="sha-trunk",
                backend="git",
                clone_url="https://github.com/acme/site.git",
                clone_protocol="https",
                dependency_paths_fingerprint="paths-a",
                checked_at=now,
                indexed_at=now,
                dependencies=(),
            ),
        ),
        touches=(),
        keep={("acme/site", "tags", "v2.0.0"), ("acme/site", "heads", "trunk")},
        repo_metadata=(
            SourceRepoMetadata(
                source_key="source:prod",
                source_repo="acme/site",
                default_branch="trunk",
            ),
        ),
        scanned_at=now,
    )

    assert set(index.cached_ref_metadata("acme/site", source_key="source:prod")) == {
        CachedRef(name="v2.0.0", kind="tags", default_branch="trunk"),
        CachedRef(name="trunk", kind="heads", default_branch="trunk"),
    }


def test_ref_scans_share_one_dependency_snapshot_for_duplicate_shas(tmp_path) -> None:
    db_path = tmp_path / "index.sqlite3"
    index = SqliteDependencyIndex(db_path)
    now = datetime(2026, 6, 1, tzinfo=UTC)

    index.commit_source_ref_refresh(
        "source:prod",
        scans=(
            RefScan(
                source_key="source:prod",
                source_repo="acme/site",
                ref_kind="heads",
                source_ref="main",
                source_sha="sha-shared",
                backend="git",
                clone_url="https://github.com/acme/site.git",
                clone_protocol="https",
                dependency_paths_fingerprint="paths-a",
                aliases_fingerprint="aliases-a",
                checked_at=now,
                indexed_at=now,
                dependencies=(
                    IndexedDependency(
                        source_repo="acme/site",
                        source_ref="main",
                        source_ref_kind="heads",
                        source_sha="sha-shared",
                        dependency_repo="acme/base",
                        dependency_name="base",
                        dependency_version=None,
                        source_path="roles/requirements.yml",
                    ),
                ),
            ),
            RefScan(
                source_key="source:prod",
                source_repo="acme/site",
                ref_kind="heads",
                source_ref="release",
                source_sha="sha-shared",
                backend="git",
                clone_url="https://github.com/acme/site.git",
                clone_protocol="https",
                dependency_paths_fingerprint="paths-a",
                aliases_fingerprint="aliases-a",
                checked_at=now,
                indexed_at=now,
                dependencies=(
                    IndexedDependency(
                        source_repo="acme/site",
                        source_ref="release",
                        source_ref_kind="heads",
                        source_sha="sha-shared",
                        dependency_repo="acme/base",
                        dependency_name="base",
                        dependency_version=None,
                        source_path="roles/requirements.yml",
                    ),
                ),
            ),
        ),
        touches=(),
        keep={("acme/site", "heads", "main"), ("acme/site", "heads", "release")},
        scanned_at=now,
    )

    assert [
        edge.source_ref for edge in index.dependencies("acme/site", None, source_key="source:prod")
    ] == ["main", "release"]
    assert {
        edge.source_ref for edge in index.dependents("acme/base", None, source_key="source:prod")
    } == {"main", "release"}
    db = sqlite3.connect(db_path)
    try:
        snapshot_count = db.execute("select count(*) from dependency_snapshots").fetchone()[0]
        edge_count = db.execute("select count(*) from snapshot_edges").fetchone()[0]
    finally:
        db.close()
    assert snapshot_count == 1
    assert edge_count == 1


def test_v1_index_tables_are_rebuilt_without_dropping_unrelated_tables(tmp_path) -> None:
    db_path = tmp_path / "index.sqlite3"
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            create table source_runs (
                source_key text primary key,
                scanned_at text not null,
                repos integer not null default 0,
                refs integer not null default 0,
                edges integer not null default 0
            );
            create table dependency_edges (
                id integer primary key autoincrement,
                source_key text not null,
                source_repo text not null,
                source_ref text,
                source_ref_kind text,
                source_sha text,
                dependency_repo text,
                dependency_name text not null,
                dependency_version text,
                source_path text not null,
                unresolved text
            );
            create table source_ref_scans (
                source_key text not null,
                source_repo text not null,
                ref_kind text not null,
                source_ref text not null,
                source_sha text not null,
                backend text not null,
                clone_url text,
                clone_protocol text,
                dependency_paths_fingerprint text not null,
                aliases_fingerprint text not null default '',
                checked_at text not null,
                indexed_at text not null,
                last_error text,
                primary key (source_key, source_repo, ref_kind, source_ref)
            );
            create table source_repo_metadata (
                source_key text not null,
                source_repo text not null,
                default_branch text not null,
                primary key (source_key, source_repo)
            );
            create table unrelated_plugin_state (
                name text primary key
            );
            insert into source_runs(source_key, scanned_at, repos, refs, edges)
            values ('source:prod', '2026-06-01T00:00:00+00:00', 1, 1, 1);
            insert into unrelated_plugin_state(name) values ('keep-me');
            """
        )
    finally:
        db.close()

    index = SqliteDependencyIndex(db_path)

    assert index.status("source:prod") is None
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("select name from unrelated_plugin_state").fetchone()[0] == "keep-me"
        source_ref_columns = {
            row[1] for row in db.execute("pragma table_info(source_ref_scans)").fetchall()
        }
    finally:
        db.close()
    assert "snapshot_id" in source_ref_columns


def test_pruning_git_refs_removes_legacy_full_source_edges(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    index.replace_source_scan(
        IndexScan(
            source_key="source:prod",
            scanned_at=now,
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    dependency_repo="acme/legacy",
                    dependency_name="legacy",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )
    index.replace_ref_scan(
        RefScan(
            source_key="source:prod",
            source_repo="acme/site",
            ref_kind="heads",
            source_ref="main",
            source_sha="sha-main",
            backend="git",
            clone_url="https://github.com/acme/site.git",
            clone_protocol="https",
            dependency_paths_fingerprint="paths-a",
            checked_at=now,
            indexed_at=now,
            dependencies=(
                IndexedDependency(
                    source_repo="acme/site",
                    source_ref="main",
                    source_sha="sha-main",
                    dependency_repo="acme/git",
                    dependency_name="git",
                    dependency_version=None,
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )

    index.prune_source_refs("source:prod", {("acme/site", "heads", "main")})

    assert not index.dependents("acme/legacy", None, source_key="source:prod")
    assert index.dependents("acme/git", None, source_key="source:prod")


def test_pruning_keeps_same_ref_name_separate_by_ref_kind(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    for ref_kind, dependency_repo in (("heads", "acme/branch-base"), ("tags", "acme/tag-base")):
        index.replace_ref_scan(
            RefScan(
                source_key="source:prod",
                source_repo="acme/site",
                ref_kind=ref_kind,
                source_ref="v1",
                source_sha=f"sha-{ref_kind}",
                backend="git",
                clone_url="https://github.com/acme/site.git",
                clone_protocol="https",
                dependency_paths_fingerprint="paths-a",
                checked_at=now,
                indexed_at=now,
                dependencies=(
                    IndexedDependency(
                        source_repo="acme/site",
                        source_ref="v1",
                        source_sha=f"sha-{ref_kind}",
                        dependency_repo=dependency_repo,
                        dependency_name=dependency_repo.rsplit("/", maxsplit=1)[-1],
                        dependency_version=None,
                        source_path="roles/requirements.yml",
                    ),
                ),
            )
        )

    index.prune_source_refs("source:prod", {("acme/site", "tags", "v1")})

    assert index.ref_scan("source:prod", "acme/site", "heads", "v1") is None
    assert index.ref_scan("source:prod", "acme/site", "tags", "v1") is not None
    assert not index.dependents("acme/branch-base", None, source_key="source:prod")
    assert index.dependents("acme/tag-base", None, source_key="source:prod")


def test_schema_column_helper_rejects_invalid_identifiers(tmp_path) -> None:
    db_path = tmp_path / "index.sqlite3"
    db = sqlite3.connect(db_path)
    try:
        with pytest.raises(ValueError, match="invalid sqlite identifier"):
            ensure_column(db, "dependency_edges; drop table source_runs", "source_sha", "text")
        with pytest.raises(ValueError, match="invalid sqlite identifier"):
            ensure_column(db, "dependency_edges", "source_sha; drop table source_runs", "text")
    finally:
        db.close()


def test_legacy_scope_schema_is_replaced(tmp_path) -> None:
    db_path = tmp_path / "index.sqlite3"
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            create table scan_runs (
                scope text primary key,
                scanned_at text not null
            );

            create table dependency_edges (
                id integer primary key autoincrement,
                scope text not null,
                source_repo text not null,
                source_ref text,
                dependency_repo text,
                dependency_name text not null,
                dependency_version text,
                source_path text not null,
                unresolved text
            );

            insert into scan_runs(scope, scanned_at)
            values ('prod', '2026-05-29T00:00:00+00:00');
            insert into dependency_edges(
                scope, source_repo, source_ref, dependency_repo, dependency_name,
                dependency_version, source_path, unresolved
            ) values (
                'prod', 'acme/site', 'main', 'acme/base', 'base', 'v1',
                'roles/requirements.yml', null
            );
            """
        )
    finally:
        db.close()

    index = SqliteDependencyIndex(db_path)

    assert index.status("source:prod") is None
    index.replace_source_scan(
        IndexScan(
            source_key="source:prod",
            scanned_at=datetime(2026, 5, 30, tzinfo=UTC),
            dependencies=(
                IndexedDependency(
                    source_repo="acme/new",
                    source_ref="main",
                    dependency_repo="acme/base",
                    dependency_name="base",
                    dependency_version="v2",
                    source_path="roles/requirements.yml",
                ),
            ),
        )
    )

    assert index.dependencies("acme/site", "main", source_key="source:prod") == []
    assert index.dependents("acme/base", "v2", source_key="source:prod")[0].source_repo == (
        "acme/new"
    )
