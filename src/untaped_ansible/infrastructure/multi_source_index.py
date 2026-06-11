"""Dependency index adapter that reads across multiple saved sources."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from untaped_ansible.domain.payloads import CachedRef, IndexedDependency

if TYPE_CHECKING:
    from untaped_ansible.application.ports import DependencyIndex

_EdgeKey = tuple[
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str,
    str | None,
    str,
    str | None,
]


class MultiSourceDependencyIndex:
    """Union dependency reads from multiple saved source cache keys."""

    def __init__(self, wrapped: DependencyIndex, source_keys: tuple[str, ...]) -> None:
        if not source_keys:
            raise ValueError("MultiSourceDependencyIndex requires at least one source key")
        self._wrapped = wrapped
        self._source_keys = tuple(dict.fromkeys(source_keys))

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        del source_key
        return _dedupe_edges(
            edge
            for selected_key in self._source_keys
            for edge in self._wrapped.dependencies(repo, ref, source_key=selected_key)
        )

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        del source_key
        return _dedupe_edges(
            edge
            for selected_key in self._source_keys
            for edge in self._wrapped.dependents(repo, ref, source_key=selected_key)
        )

    def dependencies_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        del source_key
        requested = list(dict.fromkeys(pairs))
        per_source = [
            self._wrapped.dependencies_batch(requested, source_key=selected_key)
            for selected_key in self._source_keys
        ]
        return {
            pair: _dedupe_edges(edge for loaded in per_source for edge in loaded[pair])
            for pair in requested
        }

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        del source_key
        requested = list(dict.fromkeys(pairs))
        per_source = [
            self._wrapped.dependents_batch(requested, source_key=selected_key)
            for selected_key in self._source_keys
        ]
        return {
            pair: _dedupe_edges(edge for loaded in per_source for edge in loaded[pair])
            for pair in requested
        }

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        del source_key
        refs: set[str] = set()
        for selected_key in self._source_keys:
            refs.update(self._wrapped.cached_refs(repo, source_key=selected_key))
        return refs

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        del source_key
        return _merge_ref_metadata(
            self._wrapped.cached_ref_metadata(repo, source_key=selected_key)
            for selected_key in self._source_keys
        )

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[CachedRef, ...]]:
        del source_key
        requested = list(dict.fromkeys(repos))
        per_source = [
            self._wrapped.cached_ref_metadata_batch(requested, source_key=selected_key)
            for selected_key in self._source_keys
        ]
        return {
            repo: _merge_ref_metadata(loaded[repo] for loaded in per_source) for repo in requested
        }

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        del source_key
        return any(
            self._wrapped.is_stale(selected_key, max_age_seconds=max_age_seconds)
            for selected_key in self._source_keys
        )


def _merge_ref_metadata(per_source: Iterable[tuple[CachedRef, ...]]) -> tuple[CachedRef, ...]:
    """Union cached ref metadata across sources with first-default-branch wins."""
    refs: list[CachedRef] = []
    default_branch: str | None = None
    for metadata in per_source:
        for cached_ref in metadata:
            if default_branch is None and cached_ref.default_branch is not None:
                default_branch = cached_ref.default_branch
            refs.append(cached_ref)
    deduped: list[CachedRef] = []
    seen: set[tuple[str, str | None]] = set()
    for cached_ref in refs:
        key = (cached_ref.name, cached_ref.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            cached_ref.model_copy(update={"default_branch": default_branch})
            if default_branch is not None
            else cached_ref
        )
    return tuple(deduped)


def _dedupe_edges(edges: Iterable[IndexedDependency]) -> list[IndexedDependency]:
    deduped: list[IndexedDependency] = []
    seen: set[_EdgeKey] = set()
    for edge in edges:
        key = (
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
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped
