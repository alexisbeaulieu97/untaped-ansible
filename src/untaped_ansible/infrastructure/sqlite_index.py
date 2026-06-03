"""SQLite-backed source index for reverse-impact queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from untaped_ansible.application.ports import (
    IndexedDependency,
    IndexScan,
    RefScan,
    RefScanMetadata,
    RefScanTouch,
    SourceIndexStatus,
)


class IndexStatus(SourceIndexStatus):
    """Summary of one indexed source."""


class SqliteDependencyIndex:
    """SQLite adapter satisfying the graph read port."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()

    def replace_source_scan(self, scan: IndexScan) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        repos = (
            scan.repos
            if scan.repos is not None
            else len({edge.source_repo for edge in scan.dependencies})
        )
        refs = (
            scan.refs
            if scan.refs is not None
            else len({f"{edge.source_repo}@{edge.source_ref or ''}" for edge in scan.dependencies})
        )
        edges = len(scan.dependencies)
        with self._db() as db:
            _ensure_schema(db)
            db.execute("delete from dependency_edges where source_key = ?", (scan.source_key,))
            db.execute("delete from source_runs where source_key = ?", (scan.source_key,))
            db.execute("delete from source_ref_scans where source_key = ?", (scan.source_key,))
            db.execute(
                """
                insert into source_runs(source_key, scanned_at, repos, refs, edges)
                values (?, ?, ?, ?, ?)
                """,
                (scan.source_key, _dump_dt(scan.scanned_at), repos, refs, edges),
            )
            db.executemany(
                """
                insert into dependency_edges(
                    source_key, source_repo, source_ref, source_ref_kind, source_sha,
                    dependency_repo, dependency_name, dependency_version, source_path, unresolved
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan.source_key,
                        edge.source_repo,
                        edge.source_ref,
                        None,
                        edge.source_sha,
                        edge.dependency_repo,
                        edge.dependency_name,
                        edge.dependency_version,
                        edge.source_path,
                        edge.unresolved,
                    )
                    for edge in scan.dependencies
                ],
            )

    def ref_scan(
        self,
        source_key: str,
        source_repo: str,
        ref_kind: str,
        source_ref: str,
    ) -> RefScanMetadata | None:
        with self._db() as db:
            _ensure_schema(db)
            row = db.execute(
                """
                select source_key, source_repo, ref_kind, source_ref, source_sha, backend,
                       clone_url, clone_protocol, dependency_paths_fingerprint,
                       aliases_fingerprint, checked_at, indexed_at, last_error
                from source_ref_scans
                where source_key = ? and source_repo = ? and ref_kind = ? and source_ref = ?
                """,
                (source_key, source_repo, ref_kind, source_ref),
            ).fetchone()
        if row is None:
            return None
        return _ref_scan_from_row(row)

    def ref_scans(
        self,
        source_key: str,
        source_repo: str,
        refs: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], RefScanMetadata]:
        requested = sorted(set(refs))
        if not requested:
            return {}
        scans: dict[tuple[str, str], RefScanMetadata] = {}
        with self._db() as db:
            _ensure_schema(db)
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("(?, ?)" for _ in chunk)
                params: list[object] = [
                    value for ref_kind, source_ref in chunk for value in (ref_kind, source_ref)
                ]
                params.extend((source_key, source_repo))
                rows = db.execute(
                    f"""
                    with requested(ref_kind, source_ref) as (
                        values {placeholders}
                    )
                    select scans.source_key, scans.source_repo, scans.ref_kind, scans.source_ref,
                           scans.source_sha, scans.backend, scans.clone_url, scans.clone_protocol,
                           scans.dependency_paths_fingerprint, scans.aliases_fingerprint,
                           scans.checked_at, scans.indexed_at, scans.last_error
                    from source_ref_scans as scans
                    join requested
                      on requested.ref_kind = scans.ref_kind
                     and requested.source_ref = scans.source_ref
                    where scans.source_key = ? and scans.source_repo = ?
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    key = (str(row["ref_kind"]), str(row["source_ref"]))
                    scans[key] = _ref_scan_from_row(row)
        return scans

    def replace_ref_scan(self, scan: RefScan) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            _ensure_schema(db)
            _replace_ref_scan(db, scan)

    def touch_ref_scan(
        self,
        source_key: str,
        source_repo: str,
        ref_kind: str,
        source_ref: str,
        *,
        checked_at: datetime,
    ) -> None:
        with self._db() as db:
            _ensure_schema(db)
            _touch_ref_scan(
                db,
                RefScanTouch(
                    source_key=source_key,
                    source_repo=source_repo,
                    ref_kind=ref_kind,
                    source_ref=source_ref,
                    checked_at=checked_at,
                ),
            )

    def prune_source_refs(self, source_key: str, keep: set[tuple[str, str, str]]) -> None:
        with self._db() as db:
            _ensure_schema(db)
            _prune_source_refs(db, source_key, keep)

    def commit_source_ref_refresh(
        self,
        source_key: str,
        *,
        scans: tuple[RefScan, ...],
        touches: tuple[RefScanTouch, ...],
        keep: set[tuple[str, str, str]],
        scanned_at: datetime,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            _ensure_schema(db)
            for scan in scans:
                _replace_ref_scan(db, scan)
            for touch in touches:
                _touch_ref_scan(db, touch)
            _prune_source_refs(db, source_key, keep)
            _refresh_source_run_from_ref_scans(db, source_key, scanned_at=scanned_at)

    def finalize_source_ref_scan(self, source_key: str, *, scanned_at: datetime) -> None:
        with self._db() as db:
            _ensure_schema(db)
            _refresh_source_run_from_ref_scans(db, source_key, scanned_at=scanned_at)

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        clauses = ["source_repo = ?"]
        params: list[object] = [repo]
        if ref is not None:
            clauses.append("source_ref = ?")
            params.append(ref)
        if source_key is not None:
            clauses.append("source_key = ?")
            params.append(source_key)
        return self._select_edges(clauses, params)

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        clauses = ["dependency_repo = ?"]
        params: list[object] = [repo]
        if ref is not None:
            clauses.append("dependency_version = ?")
            params.append(ref)
        if source_key is not None:
            clauses.append("source_key = ?")
            params.append(source_key)
        return self._select_edges(clauses, params)

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        if source_key is None:
            return set()
        with self._db() as db:
            _ensure_schema(db)
            rows = db.execute(
                """
                select source_ref
                from source_ref_scans
                where source_key = ? and source_repo = ?
                union
                select source_ref
                from dependency_edges
                where source_key = ? and source_repo = ? and source_ref is not null
                """,
                (source_key, repo, source_key, repo),
            ).fetchall()
        return {str(row["source_ref"]) for row in rows}

    def status(self, source_key: str) -> IndexStatus | None:
        with self._db() as db:
            _ensure_schema(db)
            row = db.execute(
                "select scanned_at, repos, refs, edges from source_runs where source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                return None
        return IndexStatus(
            source_key=source_key,
            scanned_at=_load_dt(row["scanned_at"]),
            repos=int(row["repos"]),
            refs=int(row["refs"]),
            edges=int(row["edges"]),
        )

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        if source_key is None:
            return False
        status = self.status(source_key)
        if status is None:
            return False
        age = datetime.now(UTC) - status.scanned_at
        return age.total_seconds() > max_age_seconds

    def clear(self, source_key: str | None = None) -> None:
        with self._db() as db:
            _ensure_schema(db)
            if source_key is None:
                db.execute("delete from dependency_edges")
                db.execute("delete from source_runs")
                db.execute("delete from source_ref_scans")
                return
            db.execute("delete from dependency_edges where source_key = ?", (source_key,))
            db.execute("delete from source_runs where source_key = ?", (source_key,))
            db.execute("delete from source_ref_scans where source_key = ?", (source_key,))

    def _select_edges(self, clauses: list[str], params: list[object]) -> list[IndexedDependency]:
        where = " and ".join(clauses)
        with self._db() as db:
            _ensure_schema(db)
            rows = db.execute(
                f"""
                select source_repo, source_ref, source_sha, dependency_repo, dependency_name,
                       dependency_version, source_path, unresolved
                from dependency_edges
                where {where}
                order by id
                """,
                params,
            ).fetchall()
        return [_edge_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()


def _ensure_schema(db: sqlite3.Connection) -> None:
    _drop_legacy_scope_schema(db)
    db.executescript(
        """
        create table if not exists source_runs (
            source_key text primary key,
            scanned_at text not null,
            repos integer not null default 0,
            refs integer not null default 0,
            edges integer not null default 0
        );

        create table if not exists dependency_edges (
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

        create table if not exists source_ref_scans (
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

        create index if not exists idx_dependency_edges_source
            on dependency_edges(source_key, source_repo, source_ref);
        create index if not exists idx_dependency_edges_dependency
            on dependency_edges(source_key, dependency_repo, dependency_version);
        create index if not exists idx_source_ref_scans_source
            on source_ref_scans(source_key, source_repo, ref_kind, source_ref);
        """
    )
    _ensure_column(db, "source_runs", "repos", "integer not null default 0")
    _ensure_column(db, "source_runs", "refs", "integer not null default 0")
    _ensure_column(db, "source_runs", "edges", "integer not null default 0")
    _ensure_column(db, "dependency_edges", "source_sha", "text")
    _ensure_column(db, "dependency_edges", "source_ref_kind", "text")
    _ensure_column(db, "source_ref_scans", "aliases_fingerprint", "text not null default ''")


def _drop_legacy_scope_schema(db: sqlite3.Connection) -> None:
    columns = _table_columns(db, "dependency_edges")
    if not columns or "source_key" in columns:
        return
    db.executescript(
        """
        drop table if exists dependency_edges;
        drop table if exists scan_runs;
        """
    )


def _edge_from_row(row: sqlite3.Row) -> IndexedDependency:
    return IndexedDependency(
        source_repo=row["source_repo"],
        source_ref=row["source_ref"],
        source_sha=row["source_sha"],
        dependency_repo=row["dependency_repo"],
        dependency_name=row["dependency_name"],
        dependency_version=row["dependency_version"],
        source_path=row["source_path"],
        unresolved=row["unresolved"],
    )


def _ref_scan_from_row(row: sqlite3.Row) -> RefScanMetadata:
    return RefScanMetadata(
        source_key=row["source_key"],
        source_repo=row["source_repo"],
        ref_kind=row["ref_kind"],
        source_ref=row["source_ref"],
        source_sha=row["source_sha"],
        backend=row["backend"],
        clone_url=row["clone_url"],
        clone_protocol=row["clone_protocol"],
        dependency_paths_fingerprint=row["dependency_paths_fingerprint"],
        aliases_fingerprint=row["aliases_fingerprint"],
        checked_at=_load_dt(row["checked_at"]),
        indexed_at=_load_dt(row["indexed_at"]),
        last_error=row["last_error"],
    )


def _replace_ref_scan(db: sqlite3.Connection, scan: RefScan) -> None:
    db.execute(
        """
        delete from dependency_edges
        where source_key = ? and source_repo = ? and source_ref = ?
          and source_ref_kind = ?
        """,
        (scan.source_key, scan.source_repo, scan.source_ref, scan.ref_kind),
    )
    db.executemany(
        """
        insert into dependency_edges(
            source_key, source_repo, source_ref, source_ref_kind, source_sha,
            dependency_repo, dependency_name, dependency_version, source_path, unresolved
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                scan.source_key,
                edge.source_repo,
                edge.source_ref,
                scan.ref_kind,
                edge.source_sha,
                edge.dependency_repo,
                edge.dependency_name,
                edge.dependency_version,
                edge.source_path,
                edge.unresolved,
            )
            for edge in scan.dependencies
        ],
    )
    db.execute(
        """
        insert into source_ref_scans(
            source_key, source_repo, ref_kind, source_ref, source_sha, backend,
            clone_url, clone_protocol, dependency_paths_fingerprint,
            aliases_fingerprint, checked_at, indexed_at, last_error
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(source_key, source_repo, ref_kind, source_ref) do update set
            source_sha = excluded.source_sha,
            backend = excluded.backend,
            clone_url = excluded.clone_url,
            clone_protocol = excluded.clone_protocol,
            dependency_paths_fingerprint = excluded.dependency_paths_fingerprint,
            aliases_fingerprint = excluded.aliases_fingerprint,
            checked_at = excluded.checked_at,
            indexed_at = excluded.indexed_at,
            last_error = excluded.last_error
        """,
        (
            scan.source_key,
            scan.source_repo,
            scan.ref_kind,
            scan.source_ref,
            scan.source_sha,
            scan.backend,
            scan.clone_url,
            scan.clone_protocol,
            scan.dependency_paths_fingerprint,
            scan.aliases_fingerprint,
            _dump_dt(scan.checked_at),
            _dump_dt(scan.indexed_at),
            scan.last_error,
        ),
    )


def _touch_ref_scan(db: sqlite3.Connection, touch: RefScanTouch) -> None:
    db.execute(
        """
        update source_ref_scans
        set checked_at = ?
        where source_key = ? and source_repo = ? and ref_kind = ? and source_ref = ?
        """,
        (
            _dump_dt(touch.checked_at),
            touch.source_key,
            touch.source_repo,
            touch.ref_kind,
            touch.source_ref,
        ),
    )


def _prune_source_refs(
    db: sqlite3.Connection,
    source_key: str,
    keep: set[tuple[str, str, str]],
) -> None:
    db.execute(
        """
        delete from dependency_edges
        where source_key = ? and source_ref_kind is null
        """,
        (source_key,),
    )
    rows = db.execute(
        """
        select source_repo, ref_kind, source_ref
        from source_ref_scans
        where source_key = ?
        """,
        (source_key,),
    ).fetchall()
    stale: list[tuple[str, str, str]] = []
    for row in rows:
        key = (str(row["source_repo"]), str(row["ref_kind"]), str(row["source_ref"]))
        if key not in keep:
            stale.append(key)
    if not stale:
        return
    db.executemany(
        """
        delete from dependency_edges
        where source_key = ? and source_repo = ? and source_ref = ?
          and source_ref_kind = ?
        """,
        [(source_key, repo, source_ref, ref_kind) for repo, ref_kind, source_ref in stale],
    )
    db.executemany(
        """
        delete from source_ref_scans
        where source_key = ? and source_repo = ? and ref_kind = ? and source_ref = ?
        """,
        [(source_key, repo, ref_kind, source_ref) for repo, ref_kind, source_ref in stale],
    )


def _refresh_source_run_from_ref_scans(
    db: sqlite3.Connection,
    source_key: str,
    *,
    scanned_at: datetime,
) -> None:
    row = db.execute(
        """
        select
            count(distinct source_repo) as repos,
            count(*) as refs
        from source_ref_scans
        where source_key = ?
        """,
        (source_key,),
    ).fetchone()
    refs = int(row["refs"] or 0)
    if refs == 0:
        db.execute("delete from source_runs where source_key = ?", (source_key,))
        return
    edge_row = db.execute(
        "select count(*) as edges from dependency_edges where source_key = ?",
        (source_key,),
    ).fetchone()
    db.execute(
        """
        insert into source_runs(source_key, scanned_at, repos, refs, edges)
        values (?, ?, ?, ?, ?)
        on conflict(source_key) do update set
            scanned_at = excluded.scanned_at,
            repos = excluded.repos,
            refs = excluded.refs,
            edges = excluded.edges
        """,
        (
            source_key,
            _dump_dt(scanned_at),
            int(row["repos"] or 0),
            refs,
            int(edge_row["edges"] or 0),
        ),
    )


def _dump_dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _load_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _chunks[T](values: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    _validate_sqlite_identifier(table)
    _validate_sqlite_identifier(column)
    columns = _table_columns(db, table)
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {definition}")


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    _validate_sqlite_identifier(table)
    return {row["name"] for row in db.execute(f"pragma table_info({table})")}


def _validate_sqlite_identifier(value: str) -> None:
    if not value.isidentifier():
        raise ValueError(f"invalid sqlite identifier: {value!r}")
