"""Tests for the SQLite dependency index repository."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from untaped_ansible.application.ports import IndexedDependency
from untaped_ansible.infrastructure.sqlite_index import IndexScan, SqliteDependencyIndex


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
