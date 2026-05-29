"""Tests for the SQLite dependency index repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from untaped_ansible.application.ports import IndexedDependency
from untaped_ansible.infrastructure.sqlite_index import IndexScan, SqliteDependencyIndex


def test_replace_scope_scan_supports_dependency_and_reverse_lookup(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    scanned_at = datetime(2026, 5, 29, tzinfo=UTC)

    index.replace_scope_scan(
        IndexScan(
            scope="prod",
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

    assert index.dependencies("acme/site", "main", scope="prod") == [
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
    assert index.dependents("acme/base", "v1", scope="prod")[0].source_repo == "acme/site"
    assert index.dependents("acme/base", "v2", scope="prod") == []
    assert [
        edge.dependency_name for edge in index.dependencies("acme/site", None, scope="prod")
    ] == [
        "base",
        "common",
    ]


def test_status_staleness_and_clear_scope(tmp_path) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    scanned_at = datetime.now(UTC) - timedelta(days=2)
    index.replace_scope_scan(
        IndexScan(
            scope="prod",
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

    status = index.status("prod")

    assert status is not None
    assert status.scope == "prod"
    assert status.edges == 1
    assert status.repos == 1
    assert status.refs == 1
    assert index.is_stale("prod", max_age_seconds=60)

    index.clear("prod")

    assert index.status("prod") is None
    assert not index.is_stale("prod", max_age_seconds=60)
