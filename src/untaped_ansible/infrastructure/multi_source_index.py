"""Dependency index adapter that reads across multiple saved sources."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from untaped_ansible.domain.payloads import IndexedDependency

if TYPE_CHECKING:
    from untaped_ansible.application.ports import DependencyIndex

_EdgeKey = tuple[str, str | None, str | None, str | None, str, str | None, str, str | None]


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

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        del source_key
        refs: set[str] = set()
        for selected_key in self._source_keys:
            refs.update(self._wrapped.cached_refs(repo, source_key=selected_key))
        return refs

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        del source_key
        return any(
            self._wrapped.is_stale(selected_key, max_age_seconds=max_age_seconds)
            for selected_key in self._source_keys
        )


def _dedupe_edges(edges: Iterable[IndexedDependency]) -> list[IndexedDependency]:
    deduped: list[IndexedDependency] = []
    seen: set[_EdgeKey] = set()
    for edge in edges:
        key = (
            edge.source_repo,
            edge.source_ref,
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
