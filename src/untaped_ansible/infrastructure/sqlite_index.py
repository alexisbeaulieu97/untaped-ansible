"""SQLite-backed source index for reverse-impact queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from untaped_ansible.domain.payloads import (
    CachedRef,
    IndexedDependency,
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


class SqliteDependencyIndex:
    """SQLite adapter satisfying the graph read port."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()
        self._schema_lock = Lock()
        self._schema_ready = False

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
                           scans.source_sha, scans.clone_url, scans.clone_protocol,
                           scans.dependency_paths_fingerprint, scans.aliases_fingerprint,
                           scans.checked_at, scans.indexed_at
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

    def commit_source_ref_refresh(
        self,
        source_key: str,
        *,
        scans: tuple[RefScan, ...],
        touches: tuple[RefScanTouch, ...],
        keep: set[tuple[str, str, str]],
        repo_metadata: tuple[SourceRepoMetadata, ...] = (),
        scanned_at: datetime,
        failed_repos: frozenset[str] = frozenset(),
    ) -> None:
        """Commit a refresh; ``scans`` must be unique per (source_key, source_repo,
        ref_kind, source_ref) -- duplicates raise IntegrityError inside the
        transaction instead of last-wins. Cached refs and repo metadata for
        ``failed_repos`` are preserved even though they are absent from
        ``keep``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            _replace_ref_scans(db, scans)
            _touch_ref_scans(db, touches)
            _prune_source_refs(db, source_key, keep, preserve_repos=failed_repos)
            _replace_source_repo_metadata(
                db, source_key, repo_metadata, preserve_repos=failed_repos
            )
            _refresh_source_run_from_ref_scans(db, source_key, scanned_at=scanned_at)
            _delete_orphan_snapshots(db)

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        clauses = ["scans.source_repo = ?"]
        params: list[object] = [repo]
        if ref is not None:
            clauses.append("scans.source_ref = ?")
            params.append(ref)
        if source_key is not None:
            clauses.append("scans.source_key = ?")
            params.append(source_key)
        return self._select_edges(clauses, params)

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        clauses = ["edges.dependency_repo = ?"]
        params: list[object] = [repo]
        if ref is not None:
            clauses.append("edges.dependency_version = ?")
            params.append(ref)
        if source_key is not None:
            clauses.append("scans.source_key = ?")
            params.append(source_key)
        return self._select_edges(clauses, params)

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        if source_key is None:
            return set()
        with self._db() as db:
            rows = db.execute(
                """
                select source_ref
                from source_ref_scans
                where source_key = ? and source_repo = ? and source_ref != ''
                """,
                (source_key, repo),
            ).fetchall()
        return {str(row["source_ref"]) for row in rows}

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        if source_key is None:
            return ()
        with self._db() as db:
            rows = db.execute(
                """
                select scans.source_ref as name, nullif(scans.ref_kind, '') as kind,
                       metadata.default_branch
                from source_ref_scans as scans
                left join source_repo_metadata as metadata
                  on metadata.source_key = scans.source_key
                 and metadata.source_repo = scans.source_repo
                where scans.source_key = ? and scans.source_repo = ? and scans.source_ref != ''
                order by scans.id
                """,
                (source_key, repo),
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

    def status(self, source_key: str) -> SourceIndexStatus | None:
        with self._db() as db:
            row = db.execute(
                "select scanned_at, repos, refs, edges from source_runs where source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                return None
        return SourceIndexStatus(
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
            if source_key is None:
                db.execute("delete from source_ref_scans")
                db.execute("delete from source_runs")
                db.execute("delete from source_repo_metadata")
                db.execute("delete from snapshot_edges")
                db.execute("delete from dependency_snapshots")
                return
            db.execute("delete from source_runs where source_key = ?", (source_key,))
            db.execute("delete from source_ref_scans where source_key = ?", (source_key,))
            db.execute("delete from source_repo_metadata where source_key = ?", (source_key,))
            _delete_orphan_snapshots(db)

    def _select_edges(self, clauses: list[str], params: list[object]) -> list[IndexedDependency]:
        where = " and ".join(clauses)
        with self._db() as db:
            rows = db.execute(
                f"""
                select scans.source_repo,
                       nullif(scans.source_ref, '') as source_ref,
                       nullif(scans.ref_kind, '') as source_ref_kind,
                       nullif(scans.source_sha, '') as source_sha,
                       edges.dependency_repo, edges.dependency_name, edges.dependency_version,
                       edges.source_path, edges.unresolved
                from source_ref_scans as scans
                join snapshot_edges as edges
                  on edges.snapshot_id = scans.snapshot_id
                where {where}
                order by scans.id, edges.id
                """,
                params,
            ).fetchall()
        return [edge_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        return db

    def _ensure_schema(self, db: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                ensure_schema(db, self._path)
                self._schema_ready = True

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                self._ensure_schema(db)
                yield db
        finally:
            db.close()


_SnapshotIdentity = tuple[str, str, str, str]


def _snapshot_identity(scan: RefScan) -> _SnapshotIdentity:
    return (
        scan.source_repo,
        scan.source_sha,
        scan.dependency_paths_fingerprint,
        scan.aliases_fingerprint,
    )


def _replace_ref_scans(db: sqlite3.Connection, scans: tuple[RefScan, ...]) -> None:
    if not scans:
        return
    snapshot_ids = _existing_snapshot_ids(db, scans)
    for scan in scans:
        identity = _snapshot_identity(scan)
        if identity not in snapshot_ids:
            snapshot_ids[identity] = _insert_snapshot(db, scan)
    db.executemany(
        """
        delete from source_ref_scans
        where source_key = ? and source_repo = ? and ref_kind = ? and source_ref = ?
        """,
        [(scan.source_key, scan.source_repo, scan.ref_kind, scan.source_ref) for scan in scans],
    )
    db.executemany(
        """
        insert into source_ref_scans(
            source_key, source_repo, ref_kind, source_ref, source_sha,
            clone_url, clone_protocol, dependency_paths_fingerprint,
            aliases_fingerprint, checked_at, indexed_at, snapshot_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                scan.source_key,
                scan.source_repo,
                scan.ref_kind,
                scan.source_ref,
                scan.source_sha,
                scan.clone_url,
                scan.clone_protocol,
                scan.dependency_paths_fingerprint,
                scan.aliases_fingerprint,
                dump_dt(scan.checked_at),
                dump_dt(scan.indexed_at),
                snapshot_ids[_snapshot_identity(scan)],
            )
            for scan in scans
        ],
    )


def _existing_snapshot_ids(
    db: sqlite3.Connection,
    scans: tuple[RefScan, ...],
) -> dict[_SnapshotIdentity, int]:
    identities = sorted({_snapshot_identity(scan) for scan in scans})
    ids: dict[_SnapshotIdentity, int] = {}
    # 200 identities x 4 columns = 800 bound params, under the pre-3.32 limit of
    # 999, even though this module already assumes SQLite >= 3.35 (RETURNING).
    for chunk in _chunks(identities, 200):
        placeholders = ",".join("(?, ?, ?, ?)" for _ in chunk)
        params = [value for identity in chunk for value in identity]
        rows = db.execute(
            f"""
            with requested(
                source_repo, source_sha, dependency_paths_fingerprint, aliases_fingerprint
            ) as (
                values {placeholders}
            )
            select snapshots.id, snapshots.source_repo, snapshots.source_sha,
                   snapshots.dependency_paths_fingerprint, snapshots.aliases_fingerprint
            from dependency_snapshots as snapshots
            join requested
              on requested.source_repo = snapshots.source_repo
             and requested.source_sha = snapshots.source_sha
             and requested.dependency_paths_fingerprint
                 = snapshots.dependency_paths_fingerprint
             and requested.aliases_fingerprint = snapshots.aliases_fingerprint
            """,
            params,
        ).fetchall()
        for row in rows:
            identity = (
                str(row["source_repo"]),
                str(row["source_sha"]),
                str(row["dependency_paths_fingerprint"]),
                str(row["aliases_fingerprint"]),
            )
            ids[identity] = int(row["id"])
    return ids


def _insert_snapshot(db: sqlite3.Connection, scan: RefScan) -> int:
    """Insert a new dependency snapshot with its edges and return its id."""
    row = db.execute(
        """
        insert into dependency_snapshots(
            source_repo, source_sha, dependency_paths_fingerprint, aliases_fingerprint
        ) values (?, ?, ?, ?)
        on conflict(source_repo, source_sha, dependency_paths_fingerprint, aliases_fingerprint)
        -- no-op update: only needed so RETURNING yields a row when another
        -- process inserted the same identity first (DO NOTHING returns nothing)
        do update set source_repo = excluded.source_repo
        returning id
        """,
        _snapshot_identity(scan),
    ).fetchone()
    snapshot_id = int(row["id"])
    if scan.dependencies:
        db.executemany(
            """
            insert into snapshot_edges(
                snapshot_id, dependency_repo, dependency_name, dependency_version,
                source_path, unresolved
            ) values (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    edge.dependency_repo,
                    edge.dependency_name,
                    edge.dependency_version,
                    edge.source_path,
                    edge.unresolved,
                )
                for edge in scan.dependencies
            ],
        )
    return snapshot_id


def _touch_ref_scans(db: sqlite3.Connection, touches: tuple[RefScanTouch, ...]) -> None:
    if not touches:
        return
    db.executemany(
        """
        update source_ref_scans
        set checked_at = ?
        where source_key = ? and source_repo = ? and ref_kind = ? and source_ref = ?
        """,
        [
            (
                dump_dt(touch.checked_at),
                touch.source_key,
                touch.source_repo,
                touch.ref_kind,
                touch.source_ref,
            )
            for touch in touches
        ],
    )


def _replace_source_repo_metadata(
    db: sqlite3.Connection,
    source_key: str,
    metadata: tuple[SourceRepoMetadata, ...],
    *,
    preserve_repos: frozenset[str] = frozenset(),
) -> None:
    by_repo = {row.source_repo: row for row in metadata if row.source_key == source_key}
    retained = sorted(set(by_repo) | preserve_repos)
    if not retained:
        db.execute("delete from source_repo_metadata where source_key = ?", (source_key,))
        return
    placeholders = ",".join("?" for _ in retained)
    db.execute(
        f"""
        delete from source_repo_metadata
        where source_key = ? and source_repo not in ({placeholders})
        """,
        (source_key, *retained),
    )
    if not by_repo:
        return
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
    *,
    preserve_repos: frozenset[str] = frozenset(),
) -> None:
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
        if key not in keep and key[0] not in preserve_repos:
            stale.append(key)
    if stale:
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
        """
        select count(*) as edges
        from source_ref_scans as scans
        join snapshot_edges as edges
          on edges.snapshot_id = scans.snapshot_id
        where scans.source_key = ?
        """,
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


def _delete_orphan_snapshots(db: sqlite3.Connection) -> None:
    db.execute(
        """
        delete from snapshot_edges
        where snapshot_id not in (
            select snapshot_id from source_ref_scans
        )
        """
    )
    db.execute(
        """
        delete from dependency_snapshots
        where id not in (
            select snapshot_id from source_ref_scans
        )
        """
    )


def _chunks[T](values: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
