"""Application-layer ports for dependency graph use cases."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class IndexedDependency(BaseModel):
    """One indexed dependency edge from a scanned source repo/ref."""

    model_config = ConfigDict(frozen=True)

    source_repo: str
    source_ref: str | None
    dependency_name: str
    source_path: str
    source_sha: str | None = None
    dependency_repo: str | None = None
    dependency_version: str | None = None
    unresolved: str | None = None


class IndexScan(BaseModel):
    """Complete replacement payload for one source scan."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    scanned_at: datetime
    dependencies: tuple[IndexedDependency, ...] = ()


class DependencyIndex(Protocol):
    """Read port for indexed dependency edges."""

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]: ...

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]: ...

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool: ...


class DependencyIndexWriter(Protocol):
    """Write port for replacing a source scan."""

    def replace_source_scan(self, scan: IndexScan) -> None: ...


class GitHubDependencyReader(Protocol):
    """GitHub operations needed by dependency index refresh."""

    def get_repository(self, owner: str, repo: str) -> dict[str, object]: ...

    def list_org_repos(self, org: str) -> Iterable[dict[str, object]]: ...

    def list_team_repos(self, org: str, team_slug: str) -> Iterable[dict[str, object]]: ...

    def list_matching_refs(
        self, owner: str, repo: str, namespace: str
    ) -> Iterable[dict[str, object]]: ...

    def get_tree(
        self,
        owner: str,
        repo: str,
        tree_sha: str,
        *,
        recursive: bool = False,
    ) -> dict[str, object]: ...

    def get_raw_content(self, owner: str, repo: str, path: str, *, ref: str) -> str: ...
