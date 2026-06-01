"""Dependency index adapter that overlays local dependency reads."""

from __future__ import annotations

from untaped_ansible.application.ports import DependencyIndex, IndexedDependency


class OverlayDependencyIndex(DependencyIndex):
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
        local = [
            edge
            for edge in self._local_edges
            if edge.source_repo == repo and edge.source_ref == ref
        ]
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

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self._wrapped.is_stale(source_key, max_age_seconds=max_age_seconds)
