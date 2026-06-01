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


class GitRef(BaseModel):
    """Resolved Git ref selected for dependency indexing."""

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    sha: str


class RefScanMetadata(BaseModel):
    """Freshness metadata for one indexed source repo/ref."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_repo: str
    ref_kind: str
    source_ref: str
    source_sha: str
    backend: str
    clone_url: str | None = None
    clone_protocol: str | None = None
    dependency_paths_fingerprint: str
    aliases_fingerprint: str = ""
    checked_at: datetime
    indexed_at: datetime
    last_error: str | None = None


class RefScan(RefScanMetadata):
    """Replacement payload for one source repo/ref scan."""

    dependencies: tuple[IndexedDependency, ...] = ()


class RefScanTouch(BaseModel):
    """Freshness touch for one unchanged source repo/ref scan."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_repo: str
    ref_kind: str
    source_ref: str
    checked_at: datetime


class SourceIndexStatus(BaseModel):
    """Summary of one indexed source."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    scanned_at: datetime
    repos: int
    refs: int
    edges: int


class IndexScan(BaseModel):
    """Complete replacement payload for one source scan."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    scanned_at: datetime
    repos: int | None = None
    refs: int | None = None
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

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]: ...

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool: ...


class DependencyIndexWriter(Protocol):
    """Write port for replacing a source scan."""

    def replace_source_scan(self, scan: IndexScan) -> None: ...


class IncrementalDependencyIndexWriter(DependencyIndexWriter, Protocol):
    """Write port for replacing and pruning individual source repo/ref scans."""

    def status(self, source_key: str) -> SourceIndexStatus | None: ...

    def ref_scan(
        self,
        source_key: str,
        source_repo: str,
        ref_kind: str,
        source_ref: str,
    ) -> RefScanMetadata | None: ...

    def replace_ref_scan(self, scan: RefScan) -> None: ...

    def touch_ref_scan(
        self,
        source_key: str,
        source_repo: str,
        ref_kind: str,
        source_ref: str,
        *,
        checked_at: datetime,
    ) -> None: ...

    def prune_source_refs(
        self,
        source_key: str,
        keep: set[tuple[str, str, str]],
    ) -> None: ...

    def commit_source_ref_refresh(
        self,
        source_key: str,
        *,
        scans: tuple[RefScan, ...],
        touches: tuple[RefScanTouch, ...],
        keep: set[tuple[str, str, str]],
        scanned_at: datetime,
    ) -> None: ...

    def finalize_source_ref_scan(self, source_key: str, *, scanned_at: datetime) -> None: ...


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
