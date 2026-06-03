"""Tests for reading graph data across multiple saved sources."""

from __future__ import annotations

from untaped_ansible.domain.payloads import CachedRef, IndexedDependency
from untaped_ansible.infrastructure.multi_source_index import MultiSourceDependencyIndex


class StubIndex:
    def __init__(
        self,
        edges_by_source: dict[str, list[IndexedDependency]],
        *,
        refs_by_source: dict[tuple[str, str], set[str]] | None = None,
        metadata_by_source: dict[tuple[str, str], tuple[CachedRef, ...]] | None = None,
        stale_sources: set[str] | None = None,
    ) -> None:
        self.edges_by_source = edges_by_source
        self.refs_by_source = refs_by_source or {}
        self.metadata_by_source = metadata_by_source or {}
        self.stale_sources = stale_sources or set()

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges_by_source.get(source_key or "", [])
            if edge.source_repo == repo and (ref is None or edge.source_ref == ref)
        ]

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges_by_source.get(source_key or "", [])
            if edge.dependency_repo == repo and (ref is None or edge.dependency_version == ref)
        ]

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        return set(self.refs_by_source.get((source_key or "", repo), set()))

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        return self.metadata_by_source.get((source_key or "", repo), ())

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return source_key in self.stale_sources


def test_multi_source_index_unions_dependency_reads_and_dedupes_edges() -> None:
    shared = IndexedDependency(
        source_repo="acme/site",
        source_ref="main",
        dependency_repo="acme/base",
        dependency_name="base",
        dependency_version=None,
        source_path="roles/requirements.yml",
    )
    platform_only = IndexedDependency(
        source_repo="acme/site",
        source_ref="main",
        dependency_repo="acme/common",
        dependency_name="common",
        dependency_version=None,
        source_path="meta/main.yml",
    )
    ops_only = IndexedDependency(
        source_repo="acme/site",
        source_ref="main",
        dependency_repo="acme/ops",
        dependency_name="ops",
        dependency_version=None,
        source_path="roles/requirements.yml",
    )
    index = MultiSourceDependencyIndex(
        StubIndex(
            {
                "source:platform": [shared, platform_only],
                "source:ops": [shared, ops_only],
            }
        ),
        ("source:platform", "source:ops"),
    )

    assert index.dependencies("acme/site", "main", source_key="sources:platform,ops") == [
        shared,
        platform_only,
        ops_only,
    ]


def test_multi_source_index_unions_dependents_cached_refs_and_staleness() -> None:
    platform_edge = IndexedDependency(
        source_repo="acme/site",
        source_ref="main",
        dependency_repo="acme/base",
        dependency_name="base",
        dependency_version="v1",
        source_path="roles/requirements.yml",
    )
    ops_edge = IndexedDependency(
        source_repo="acme/deploy",
        source_ref="release",
        dependency_repo="acme/base",
        dependency_name="base",
        dependency_version="v1",
        source_path="roles/requirements.yml",
    )
    index = MultiSourceDependencyIndex(
        StubIndex(
            {
                "source:platform": [platform_edge],
                "source:ops": [ops_edge],
            },
            refs_by_source={
                ("source:platform", "acme/site"): {"main"},
                ("source:ops", "acme/site"): {"release"},
            },
            stale_sources={"source:ops"},
        ),
        ("source:platform", "source:ops"),
    )

    assert index.dependents("acme/base", "v1", source_key="sources:platform,ops") == [
        platform_edge,
        ops_edge,
    ]
    assert index.cached_refs("acme/site", source_key="sources:platform,ops") == {
        "main",
        "release",
    }
    assert index.is_stale("sources:platform,ops", max_age_seconds=60)


def test_multi_source_index_unions_cached_ref_metadata_with_first_default_branch() -> None:
    index = MultiSourceDependencyIndex(
        StubIndex(
            {},
            metadata_by_source={
                ("source:platform", "acme/site"): (
                    CachedRef(name="main", kind="heads", default_branch="main"),
                    CachedRef(name="v1.0.0", kind="tags", default_branch="main"),
                ),
                ("source:ops", "acme/site"): (
                    CachedRef(name="release", kind="heads", default_branch="release"),
                    CachedRef(name="v2.0.0", kind="tags", default_branch="release"),
                ),
            },
        ),
        ("source:platform", "source:ops"),
    )

    assert index.cached_ref_metadata("acme/site", source_key="sources:platform,ops") == (
        CachedRef(name="main", kind="heads", default_branch="main"),
        CachedRef(name="v1.0.0", kind="tags", default_branch="main"),
        CachedRef(name="release", kind="heads", default_branch="main"),
        CachedRef(name="v2.0.0", kind="tags", default_branch="main"),
    )
