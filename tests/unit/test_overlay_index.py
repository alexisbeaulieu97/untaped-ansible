"""Tests for the local-overlay dependency index adapter."""

from __future__ import annotations

from collections.abc import Sequence

from untaped_ansible.domain.payloads import CachedRef, IndexedDependency
from untaped_ansible.infrastructure.overlay_index import OverlayDependencyIndex


def _edge(
    source_repo: str,
    source_ref: str | None,
    dependency_repo: str,
) -> IndexedDependency:
    return IndexedDependency(
        source_repo=source_repo,
        source_ref=source_ref,
        dependency_repo=dependency_repo,
        dependency_name=dependency_repo.partition("/")[2],
        dependency_version="main",
        source_path="roles/requirements.yml",
    )


class StubIndex:
    def __init__(
        self,
        edges: list[IndexedDependency],
        *,
        cached_ref_metadata: dict[str, tuple[CachedRef, ...]] | None = None,
    ) -> None:
        self.edges = edges
        self._cached_ref_metadata = cached_ref_metadata or {}

    def dependencies(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges
            if edge.source_repo == repo and (ref is None or edge.source_ref == ref)
        ]

    def dependents(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges
            if edge.dependency_repo == repo and (ref is None or edge.dependency_version == ref)
        ]

    def dependencies_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        return {
            (repo, ref): self.dependencies(repo, ref, source_key=source_key) for repo, ref in pairs
        }

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        return {
            (repo, ref): self.dependents(repo, ref, source_key=source_key) for repo, ref in pairs
        }

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        return {cached_ref.name for cached_ref in self._cached_ref_metadata.get(repo, ())}

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        return self._cached_ref_metadata.get(repo, ())

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[CachedRef, ...]]:
        return {repo: self.cached_ref_metadata(repo, source_key=source_key) for repo in repos}

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return False


def test_dependencies_prefer_local_edges_and_keep_authoritative_pairs_local() -> None:
    local = _edge("acme/site", "main", "acme/local-base")
    indexed = _edge("acme/site", "release", "acme/indexed-base")
    index = OverlayDependencyIndex(
        StubIndex([indexed]),
        [local],
        authoritative_sources={("acme/site", "main")},
    )

    assert index.dependencies("acme/site", "main", source_key=None) == [local]
    assert index.dependencies("acme/site", "release", source_key=None) == [indexed]
    assert index.dependents("acme/indexed-base", None, source_key=None) == [indexed]


def test_dependencies_batch_mixes_local_authoritative_and_delegated_pairs() -> None:
    local = _edge("acme/site", "main", "acme/local-base")
    indexed = _edge("acme/other", "main", "acme/indexed-base")
    index = OverlayDependencyIndex(
        StubIndex([indexed]),
        [local],
        authoritative_sources={("acme/site", "main"), ("acme/empty", "v1")},
    )

    batch = index.dependencies_batch(
        [("acme/site", "main"), ("acme/empty", "v1"), ("acme/other", "main")],
        source_key=None,
    )

    assert batch[("acme/site", "main")] == [local]
    assert batch[("acme/empty", "v1")] == []
    assert batch[("acme/other", "main")] == [indexed]


def test_dependents_batch_delegates_to_wrapped_index() -> None:
    indexed = _edge("acme/site", "main", "acme/base")
    index = OverlayDependencyIndex(StubIndex([indexed]), [])

    batch = index.dependents_batch(
        [("acme/base", "main"), ("acme/missing", None)],
        source_key=None,
    )

    assert batch[("acme/base", "main")] == [indexed]
    assert batch[("acme/missing", None)] == []


def test_cached_ref_reads_overlay_authoritative_refs_onto_wrapped_metadata() -> None:
    cached = CachedRef(name="release", kind="heads", default_branch="release")
    index = OverlayDependencyIndex(
        StubIndex([], cached_ref_metadata={"acme/site": (cached,)}),
        [],
        authoritative_sources={("acme/site", "main"), ("acme/site", None)},
    )

    assert index.cached_refs("acme/site", source_key=None) == {"release", "main"}
    assert index.cached_ref_metadata("acme/site", source_key=None) == (
        cached,
        CachedRef(name="main"),
    )
    assert index.cached_ref_metadata_batch(["acme/site", "acme/other"], source_key=None) == {
        "acme/site": (cached, CachedRef(name="main")),
        "acme/other": (),
    }
    assert not index.is_stale(None, max_age_seconds=60)
