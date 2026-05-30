"""SQLite-backed source index for reverse-impact queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from untaped_ansible.application.ports import IndexedDependency, IndexScan


class IndexStatus(BaseModel):
    """Summary of one indexed source."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    scanned_at: datetime
    repos: int
    refs: int
    edges: int


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
                    source_key, source_repo, source_ref, source_sha, dependency_repo,
                    dependency_name, dependency_version, source_path, unresolved
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan.source_key,
                        edge.source_repo,
                        edge.source_ref,
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
                return
            db.execute("delete from dependency_edges where source_key = ?", (source_key,))
            db.execute("delete from source_runs where source_key = ?", (source_key,))

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
            source_sha text,
            dependency_repo text,
            dependency_name text not null,
            dependency_version text,
            source_path text not null,
            unresolved text
        );

        create index if not exists idx_dependency_edges_source
            on dependency_edges(source_key, source_repo, source_ref);
        create index if not exists idx_dependency_edges_dependency
            on dependency_edges(source_key, dependency_repo, dependency_version);
        """
    )
    _ensure_column(db, "source_runs", "repos", "integer not null default 0")
    _ensure_column(db, "source_runs", "refs", "integer not null default 0")
    _ensure_column(db, "source_runs", "edges", "integer not null default 0")
    _ensure_column(db, "dependency_edges", "source_sha", "text")


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


def _dump_dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _load_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = _table_columns(db, table)
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {definition}")


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"pragma table_info({table})")}
