"""Application-layer ports for dependency graph use cases."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from untaped_ansible.domain import payloads


class DependencyIndex(Protocol):
    """Read port for indexed dependency edges."""

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[payloads.IndexedDependency]: ...

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[payloads.IndexedDependency]: ...

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]: ...

    def cached_ref_metadata(
        self, repo: str, *, source_key: str | None
    ) -> tuple[payloads.CachedRef, ...]: ...

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool: ...


class DependencyIndexWriter(Protocol):
    """Write port for replacing a source scan."""

    def replace_source_scan(self, scan: payloads.IndexScan) -> None: ...


class IncrementalDependencyIndexWriter(DependencyIndexWriter, Protocol):
    """Write port for replacing and pruning individual source repo/ref scans."""

    def status(self, source_key: str) -> payloads.SourceIndexStatus | None: ...

    def ref_scan(
        self,
        source_key: str,
        source_repo: str,
        ref_kind: str,
        source_ref: str,
    ) -> payloads.RefScanMetadata | None: ...

    def ref_scans(
        self,
        source_key: str,
        source_repo: str,
        refs: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], payloads.RefScanMetadata]: ...

    def replace_ref_scan(self, scan: payloads.RefScan) -> None: ...

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
        scans: tuple[payloads.RefScan, ...],
        touches: tuple[payloads.RefScanTouch, ...],
        keep: set[tuple[str, str, str]],
        repo_metadata: tuple[payloads.SourceRepoMetadata, ...] = (),
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
