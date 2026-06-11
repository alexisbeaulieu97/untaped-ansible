"""Application-layer ports for dependency graph use cases."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Protocol

from untaped_ansible.domain import payloads


class DependencyIndex(Protocol):
    """Read port for indexed dependency edges.

    Batch reads bulk-load many keys in one call for level-batched graph
    traversal. Every requested key must appear in the returned mapping --
    with an empty list/tuple when nothing is indexed -- so callers can cache
    negative results. A ``None`` ref in a ``(repo, ref)`` pair keeps the
    single-read semantics: edges for every indexed ref of that repo.
    """

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

    def dependencies_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[payloads.IndexedDependency]]: ...

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[payloads.IndexedDependency]]: ...

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]: ...

    def cached_ref_metadata(
        self, repo: str, *, source_key: str | None
    ) -> tuple[payloads.CachedRef, ...]: ...

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[payloads.CachedRef, ...]]: ...

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool: ...


class IncrementalDependencyIndexWriter(Protocol):
    """Write port for committing source repo/ref refreshes."""

    def status(self, source_key: str) -> payloads.SourceIndexStatus | None: ...

    def ref_scans(
        self,
        source_key: str,
        source_repo: str,
        refs: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], payloads.RefScanMetadata]: ...

    def commit_source_ref_refresh(
        self,
        source_key: str,
        *,
        scans: tuple[payloads.RefScan, ...],
        touches: tuple[payloads.RefScanTouch, ...],
        keep: set[tuple[str, str, str]],
        repo_metadata: tuple[payloads.SourceRepoMetadata, ...] = (),
        scanned_at: datetime,
        failed_repos: frozenset[str] = frozenset(),
    ) -> None:
        """Commit a refresh; ``scans`` must be unique per (source_key, source_repo,
        ref_kind, source_ref) -- duplicates raise IntegrityError inside the
        transaction instead of last-wins. Refs and repo metadata cached for
        ``failed_repos`` survive the prune even though they contribute nothing
        to ``keep``."""
        ...


class RefProbe(Protocol):
    """Batched remote ref freshness probe for many repositories."""

    def probe(
        self,
        repos: Sequence[str],
        *,
        kinds: Sequence[str],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> payloads.ProbeReport: ...


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
