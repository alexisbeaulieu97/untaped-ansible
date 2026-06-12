"""Dependency index adapter that overlays local dependency reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from untaped_ansible.domain.payloads import CachedRef, IndexedDependency

if TYPE_CHECKING:
    from collections.abc import Sequence

    from untaped_ansible.application.ports import DependencyIndex


class OverlayDependencyIndex:
    """Prefer local dependency edges while delegating reverse-impact reads."""

    def __init__(
        self,
        wrapped: DependencyIndex,
        local_edges: list[IndexedDependency],
        *,
        authoritative_sources: set[tuple[str, str | None]] | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._local_edges = local_edges
        self._authoritative_sources = authoritative_sources or set()

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        local = self._local_dependencies(repo, ref)
        if (repo, ref) in self._authoritative_sources:
            return local
        return local or self._wrapped.dependencies(repo, ref, source_key=source_key)

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return self._wrapped.dependents(repo, ref, source_key=source_key)

    def dependencies_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        results: dict[tuple[str, str | None], list[IndexedDependency]] = {}
        delegated: list[tuple[str, str | None]] = []
        for repo, ref in dict.fromkeys(pairs):
            local = self._local_dependencies(repo, ref)
            if local or (repo, ref) in self._authoritative_sources:
                results[(repo, ref)] = local
            else:
                delegated.append((repo, ref))
        if delegated:
            results.update(self._wrapped.dependencies_batch(delegated, source_key=source_key))
        return results

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        return self._wrapped.dependents_batch(pairs, source_key=source_key)

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        refs = set(self._wrapped.cached_refs(repo, source_key=source_key))
        refs.update(
            ref
            for source_repo, ref in self._authoritative_sources
            if source_repo == repo and ref is not None
        )
        return refs

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        return self._overlay_ref_metadata(
            repo,
            self._wrapped.cached_ref_metadata(repo, source_key=source_key),
        )

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[CachedRef, ...]]:
        wrapped = self._wrapped.cached_ref_metadata_batch(repos, source_key=source_key)
        return {
            repo: self._overlay_ref_metadata(repo, metadata) for repo, metadata in wrapped.items()
        }

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self._wrapped.is_stale(source_key, max_age_seconds=max_age_seconds)

    def _local_dependencies(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        return [
            edge
            for edge in self._local_edges
            if edge.source_repo == repo and edge.source_ref == ref
        ]

    def _overlay_ref_metadata(
        self,
        repo: str,
        wrapped: tuple[CachedRef, ...],
    ) -> tuple[CachedRef, ...]:
        metadata = list(wrapped)
        known = {(cached_ref.name, cached_ref.kind) for cached_ref in metadata}
        for source_repo, ref in self._authoritative_sources:
            if source_repo != repo or ref is None or (ref, None) in known:
                continue
            metadata.append(CachedRef(name=ref))
        return tuple(metadata)
