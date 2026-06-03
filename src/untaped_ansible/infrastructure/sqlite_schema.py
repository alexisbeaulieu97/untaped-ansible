"""SQLite schema and migration helpers for the dependency index."""

from __future__ import annotations

import sqlite3


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create and migrate dependency index tables."""
    _drop_legacy_schema(db)
    db.executescript(
        """
        create table if not exists source_runs (
            source_key text primary key,
            scanned_at text not null,
            repos integer not null default 0,
            refs integer not null default 0,
            edges integer not null default 0
        );

        create table if not exists dependency_snapshots (
            id integer primary key autoincrement,
            source_repo text not null,
            source_sha text,
            dependency_paths_fingerprint text not null,
            aliases_fingerprint text not null default '',
            unique(source_repo, source_sha, dependency_paths_fingerprint, aliases_fingerprint)
        );

        create table if not exists snapshot_edges (
            id integer primary key autoincrement,
            snapshot_id integer not null references dependency_snapshots(id) on delete cascade,
            dependency_repo text,
            dependency_name text not null,
            dependency_version text,
            source_path text not null,
            unresolved text
        );

        create table if not exists source_ref_scans (
            id integer primary key autoincrement,
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
            snapshot_id integer not null references dependency_snapshots(id),
            unique(source_key, source_repo, ref_kind, source_ref)
        );

        create table if not exists source_repo_metadata (
            source_key text not null,
            source_repo text not null,
            default_branch text not null,
            primary key (source_key, source_repo)
        );

        create index if not exists idx_dependency_snapshots_identity
            on dependency_snapshots(
                source_repo, source_sha, dependency_paths_fingerprint, aliases_fingerprint
            );
        create index if not exists idx_snapshot_edges_dependency
            on snapshot_edges(snapshot_id, dependency_repo, dependency_version);
        create index if not exists idx_source_ref_scans_source
            on source_ref_scans(source_key, source_repo, ref_kind, source_ref);
        create index if not exists idx_source_ref_scans_snapshot
            on source_ref_scans(snapshot_id);
        create index if not exists idx_source_repo_metadata_source
            on source_repo_metadata(source_key, source_repo);
        """
    )


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column when it is missing, after validating identifier inputs."""
    _validate_sqlite_identifier(table)
    _validate_sqlite_identifier(column)
    columns = _table_columns(db, table)
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {definition}")


def _drop_legacy_schema(db: sqlite3.Connection) -> None:
    source_ref_columns = _table_columns(db, "source_ref_scans")
    has_v1_ref_scans = bool(source_ref_columns) and "snapshot_id" not in source_ref_columns
    has_legacy_scope_edges = _legacy_dependency_edges(db)
    if not has_v1_ref_scans and not has_legacy_scope_edges:
        return
    db.executescript(
        """
        drop table if exists dependency_edges;
        drop table if exists scan_runs;
        drop table if exists source_runs;
        drop table if exists source_ref_scans;
        drop table if exists source_repo_metadata;
        drop table if exists snapshot_edges;
        drop table if exists dependency_snapshots;
        """
    )


def _legacy_dependency_edges(db: sqlite3.Connection) -> bool:
    columns = _table_columns(db, "dependency_edges")
    return bool(columns) and "source_key" not in columns


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    _validate_sqlite_identifier(table)
    return {row["name"] for row in db.execute(f"pragma table_info({table})")}


def _validate_sqlite_identifier(value: str) -> None:
    if not value.isidentifier():
        raise ValueError(f"invalid sqlite identifier: {value!r}")
