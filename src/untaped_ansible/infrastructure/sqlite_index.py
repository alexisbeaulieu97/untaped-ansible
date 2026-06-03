"""SQLite-backed source index for reverse-impact queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from untaped_ansible.domain.payloads import (
    CachedRef,
    IndexedDependency,
    IndexScan,
    RefScan,
    RefScanMetadata,
    RefScanTouch,
    SourceIndexStatus,
    SourceRepoMetadata,
)
from untaped_ansible.infrastructure.sqlite_rows import (
    dump_dt,
    edge_from_row,
    load_dt,
    ref_scan_from_row,
)
from untaped_ansible.infrastructure.sqlite_schema import ensure_schema


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
            ensure_schema(db)
            db.execute("delete from dependency_edges where source_key = ?", (scan.source_key,))
            db.execute("delete from source_runs where source_key = ?", (scan.source_key,))
            db.execute("delete from source_ref_scans where source_key = ?", (scan.source_key,))
            db.execute("delete from source_repo_metadata where source_key = ?", (scan.source_key,))
            db.execute(
                """
                insert into source_runs(source_key, scanned_at, repos, refs, edges)
                values (?, ?, ?, ?, ?)
                """,
                (scan.source_key, dump_dt(scan.scanned_at), repos, refs, edges),
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
                        edge.source_ref_kind,
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
            _replace_source_repo_metadata(db, scan.source_key, scan.repo_metadata)

    def ref_scan(
        self,
        source_key: str,
        source_repo: str,
        ref_kind: str,
        source_ref: str,
    ) -> RefScanMetadata | None:
        with self._db() as db:
            ensure_schema(db)
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
        return ref_scan_from_row(row)

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
            ensure_schema(db)
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
                    scans[key] = ref_scan_from_row(row)
        return scans

    def replace_ref_scan(self, scan: RefScan) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            ensure_schema(db)
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
            ensure_schema(db)
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
            ensure_schema(db)
            _prune_source_refs(db, source_key, keep)

    def commit_source_ref_refresh(
        self,
        source_key: str,
        *,
        scans: tuple[RefScan, ...],
        touches: tuple[RefScanTouch, ...],
        keep: set[tuple[str, str, str]],
        repo_metadata: tuple[SourceRepoMetadata, ...] = (),
        scanned_at: datetime,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            ensure_schema(db)
            for scan in scans:
                _replace_ref_scan(db, scan)
            for touch in touches:
                _touch_ref_scan(db, touch)
            _prune_source_refs(db, source_key, keep)
            _replace_source_repo_metadata(db, source_key, repo_metadata)
            _refresh_source_run_from_ref_scans(db, source_key, scanned_at=scanned_at)

    def finalize_source_ref_scan(self, source_key: str, *, scanned_at: datetime) -> None:
        with self._db() as db:
            ensure_schema(db)
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
            ensure_schema(db)
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

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        if source_key is None:
            return ()
        with self._db() as db:
            ensure_schema(db)
            rows = db.execute(
                """
                select scans.source_ref as name, scans.ref_kind as kind, metadata.default_branch
                from source_ref_scans as scans
                left join source_repo_metadata as metadata
                  on metadata.source_key = scans.source_key
                 and metadata.source_repo = scans.source_repo
                where scans.source_key = ? and scans.source_repo = ?
                union all
                select edges.source_ref as name, edges.source_ref_kind as kind,
                       metadata.default_branch
                from dependency_edges as edges
                left join source_repo_metadata as metadata
                  on metadata.source_key = edges.source_key
                 and metadata.source_repo = edges.source_repo
                where edges.source_key = ? and edges.source_repo = ?
                  and edges.source_ref is not null
                """,
                (source_key, repo, source_key, repo),
            ).fetchall()
        refs: dict[tuple[str, str | None], CachedRef] = {}
        for row in rows:
            key = (str(row["name"]), _optional_str(row["kind"]))
            refs.setdefault(
                key,
                CachedRef(
                    name=key[0],
                    kind=key[1],
                    default_branch=_optional_str(row["default_branch"]),
                ),
            )
        return tuple(refs.values())

    def status(self, source_key: str) -> IndexStatus | None:
        with self._db() as db:
            ensure_schema(db)
            row = db.execute(
                "select scanned_at, repos, refs, edges from source_runs where source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                return None
        return IndexStatus(
            source_key=source_key,
            scanned_at=load_dt(row["scanned_at"]),
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
            ensure_schema(db)
            if source_key is None:
                db.execute("delete from dependency_edges")
                db.execute("delete from source_runs")
                db.execute("delete from source_ref_scans")
                db.execute("delete from source_repo_metadata")
                return
            db.execute("delete from dependency_edges where source_key = ?", (source_key,))
            db.execute("delete from source_runs where source_key = ?", (source_key,))
            db.execute("delete from source_ref_scans where source_key = ?", (source_key,))
            db.execute("delete from source_repo_metadata where source_key = ?", (source_key,))

    def _select_edges(self, clauses: list[str], params: list[object]) -> list[IndexedDependency]:
        where = " and ".join(clauses)
        with self._db() as db:
            ensure_schema(db)
            rows = db.execute(
                f"""
                select source_repo, source_ref, source_ref_kind, source_sha, dependency_repo,
                       dependency_name, dependency_version, source_path, unresolved
                from dependency_edges
                where {where}
                order by id
                """,
                params,
            ).fetchall()
        return [edge_from_row(row) for row in rows]

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
            dump_dt(scan.checked_at),
            dump_dt(scan.indexed_at),
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
            dump_dt(touch.checked_at),
            touch.source_key,
            touch.source_repo,
            touch.ref_kind,
            touch.source_ref,
        ),
    )


def _replace_source_repo_metadata(
    db: sqlite3.Connection,
    source_key: str,
    metadata: tuple[SourceRepoMetadata, ...],
) -> None:
    by_repo = {row.source_repo: row for row in metadata if row.source_key == source_key}
    if not by_repo:
        db.execute("delete from source_repo_metadata where source_key = ?", (source_key,))
        return
    placeholders = ",".join("?" for _ in by_repo)
    db.execute(
        f"""
        delete from source_repo_metadata
        where source_key = ? and source_repo not in ({placeholders})
        """,
        (source_key, *by_repo),
    )
    db.executemany(
        """
        insert into source_repo_metadata(source_key, source_repo, default_branch)
        values (?, ?, ?)
        on conflict(source_key, source_repo) do update set
            default_branch = excluded.default_branch
        """,
        [
            (source_key, row.source_repo, row.default_branch)
            for row in sorted(by_repo.values(), key=lambda item: item.source_repo)
        ],
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
            dump_dt(scanned_at),
            int(row["repos"] or 0),
            refs,
            int(edge_row["edges"] or 0),
        ),
    )


def _chunks[T](values: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
