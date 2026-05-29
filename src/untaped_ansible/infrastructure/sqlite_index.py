"""SQLite-backed dependency index for reverse-impact queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from untaped_ansible.application.ports import IndexedDependency, IndexScan


class IndexStatus(BaseModel):
    """Summary of one indexed scope."""

    model_config = ConfigDict(frozen=True)

    scope: str
    scanned_at: datetime
    repos: int
    refs: int
    edges: int


class SqliteDependencyIndex:
    """SQLite adapter satisfying the graph read port."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()

    def replace_scope_scan(self, scan: IndexScan) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            _ensure_schema(db)
            db.execute("delete from dependency_edges where scope = ?", (scan.scope,))
            db.execute("delete from scan_runs where scope = ?", (scan.scope,))
            db.execute(
                "insert into scan_runs(scope, scanned_at) values (?, ?)",
                (scan.scope, _dump_dt(scan.scanned_at)),
            )
            db.executemany(
                """
                insert into dependency_edges(
                    scope, source_repo, source_ref, source_sha, dependency_repo, dependency_name,
                    dependency_version, source_path, unresolved
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan.scope,
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
        scope: str | None,
    ) -> list[IndexedDependency]:
        clauses = ["source_repo = ?"]
        params: list[object] = [repo]
        if ref is not None:
            clauses.append("source_ref = ?")
            params.append(ref)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        return self._select_edges(clauses, params)

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        scope: str | None,
    ) -> list[IndexedDependency]:
        clauses = ["dependency_repo = ?"]
        params: list[object] = [repo]
        if ref is not None:
            clauses.append("dependency_version = ?")
            params.append(ref)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        return self._select_edges(clauses, params)

    def status(self, scope: str) -> IndexStatus | None:
        with self._db() as db:
            _ensure_schema(db)
            row = db.execute(
                "select scanned_at from scan_runs where scope = ?",
                (scope,),
            ).fetchone()
            if row is None:
                return None
            counts = db.execute(
                """
                select
                    count(distinct source_repo) as repos,
                    count(distinct source_repo || '@' || coalesce(source_ref, '')) as refs,
                    count(*) as edges
                from dependency_edges
                where scope = ?
                """,
                (scope,),
            ).fetchone()
        return IndexStatus(
            scope=scope,
            scanned_at=_load_dt(row["scanned_at"]),
            repos=int(counts["repos"]),
            refs=int(counts["refs"]),
            edges=int(counts["edges"]),
        )

    def is_stale(self, scope: str | None, *, max_age_seconds: int) -> bool:
        if scope is None:
            return False
        status = self.status(scope)
        if status is None:
            return False
        age = datetime.now(UTC) - status.scanned_at
        return age.total_seconds() > max_age_seconds

    def clear(self, scope: str | None = None) -> None:
        with self._db() as db:
            _ensure_schema(db)
            if scope is None:
                db.execute("delete from dependency_edges")
                db.execute("delete from scan_runs")
                return
            db.execute("delete from dependency_edges where scope = ?", (scope,))
            db.execute("delete from scan_runs where scope = ?", (scope,))

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
    db.executescript(
        """
        create table if not exists scan_runs (
            scope text primary key,
            scanned_at text not null
        );

        create table if not exists dependency_edges (
            id integer primary key autoincrement,
            scope text not null,
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
            on dependency_edges(scope, source_repo, source_ref);
        create index if not exists idx_dependency_edges_dependency
            on dependency_edges(scope, dependency_repo, dependency_version);
        """
    )
    _ensure_column(db, "dependency_edges", "source_sha", "text")


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
    columns = {row["name"] for row in db.execute(f"pragma table_info({table})")}
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {definition}")
