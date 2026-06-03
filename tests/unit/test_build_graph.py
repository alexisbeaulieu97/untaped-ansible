"""Tests for dependency and impact graph construction."""

from __future__ import annotations

from untaped_ansible.application.graph import BuildGraph, GraphRequest
from untaped_ansible.application.ports import IndexedDependency


class StubIndex:
    def __init__(
        self,
        edges: list[IndexedDependency],
        *,
        cached_refs: dict[str, set[str]] | None = None,
        stale: bool = False,
    ) -> None:
        self.edges = edges
        self.stale = stale
        if cached_refs is None:
            cached_refs = {}
            for edge in edges:
                if edge.source_ref is not None:
                    cached_refs.setdefault(edge.source_repo, set()).add(edge.source_ref)
        self._cached_refs = cached_refs
        self.dependency_calls: dict[tuple[str, str | None, str | None], int] = {}
        self.dependent_calls: dict[tuple[str, str | None, str | None], int] = {}
        self.cached_refs_calls: dict[tuple[str, str | None], int] = {}

    def dependencies(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        key = (repo, ref, source_key)
        self.dependency_calls[key] = self.dependency_calls.get(key, 0) + 1
        return [
            edge
            for edge in self.edges
            if edge.source_repo == repo and (ref is None or edge.source_ref == ref)
        ]

    def dependents(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        key = (repo, ref, source_key)
        self.dependent_calls[key] = self.dependent_calls.get(key, 0) + 1
        return [
            edge
            for edge in self.edges
            if edge.dependency_repo == repo and (ref is None or edge.dependency_version == ref)
        ]

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self.stale

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        key = (repo, source_key)
        self.cached_refs_calls[key] = self.cached_refs_calls.get(key, 0) + 1
        if source_key is None:
            return set()
        return set(self._cached_refs.get(repo, set()))


def test_build_graph_includes_dependencies_impact_unresolved_and_stale_warning() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/base",
                source_ref="v1",
                dependency_repo="acme/users",
                dependency_name="users",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/base",
                source_ref="v1",
                dependency_name="common",
                dependency_version=None,
                source_path="meta/main.yml",
                unresolved="common",
            ),
            IndexedDependency(
                source_repo="acme/site",
                source_ref="release/1",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
        ],
        stale=True,
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/base",
            ref="v1",
            source_key="source:prod",
            direction="both",
            depth=1,
        )
    )

    assert [node.label for node in graph.nodes] == [
        "acme/base@v1",
        "acme/users@main",
        "unresolved: common",
        "acme/site@release/1",
    ]
    assert [(edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        ("acme/base@v1", "acme/users@main", "requires"),
        ("acme/base@v1", "unresolved:common", "requires"),
        ("acme/site@release/1", "acme/base@v1", "impacts"),
    ]
    assert graph.warnings == (
        "source data is stale; refresh it before relying on upstream impact",
        "unresolved dependency common from acme/base@v1 in meta/main.yml",
    )


def test_depth_limits_transitive_dependency_traversal_and_avoids_cycles() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/base",
                source_ref="v1",
                dependency_repo="acme/users",
                dependency_name="users",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/users",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v1",
                source_path="meta/main.yml",
            ),
        ]
    )

    graph = BuildGraph(index)(GraphRequest(repo="acme/base", ref="v1", direction="deps", depth=3))

    assert len(graph.edges) == 1
    assert graph.edges[0].target_id == "acme/users@main"


def test_transitive_dependency_traversal_uses_exact_cached_refs() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/a",
                source_ref="main",
                dependency_repo="acme/b",
                dependency_name="b",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/b",
                source_ref="v1",
                dependency_repo="acme/c",
                dependency_name="c",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={
            "acme/a": {"main"},
            "acme/b": {"v1"},
            "acme/c": {"main"},
        },
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/a",
            ref="main",
            source_key="source:prod",
            direction="deps",
            depth=3,
        )
    )

    assert [(edge.source_id, edge.target_id) for edge in graph.edges] == [
        ("acme/a@main", "acme/b@v1"),
        ("acme/b@v1", "acme/c@main"),
    ]
    assert graph.warnings == ()


def test_downstream_without_ref_keeps_each_matching_target_ref() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/base",
                source_ref="main",
                dependency_repo="acme/users",
                dependency_name="users",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/base",
                source_ref="v1",
                dependency_repo="acme/legacy",
                dependency_name="legacy",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={
            "acme/base": {"main", "v1"},
            "acme/users": {"v1"},
            "acme/legacy": {"v1"},
        },
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/base",
            ref=None,
            source_key="source:prod",
            direction="deps",
            depth=1,
        )
    )

    assert graph.target_id == "acme/base"
    assert [(edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        ("acme/base@main", "acme/users@v1", "requires"),
        ("acme/base@v1", "acme/legacy@v1", "requires"),
    ]


def test_transitive_dependency_traversal_warns_and_stops_when_ref_is_not_cached() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/a",
                source_ref="main",
                dependency_repo="acme/b",
                dependency_name="b",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/b",
                source_ref="main",
                dependency_repo="acme/c",
                dependency_name="c",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={"acme/a": {"main"}, "acme/b": {"main"}},
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/a",
            ref="main",
            source_key="source:prod",
            direction="deps",
            depth=3,
        )
    )

    assert [(edge.source_id, edge.target_id) for edge in graph.edges] == [
        ("acme/a@main", "acme/b@v1"),
    ]
    assert graph.warnings == (
        "not expanding acme/b@v1 from cached source data: ref is not cached "
        "(available refs: main). Scan the matching ref/tag or use --live for downstream.",
    )


def test_both_direction_warns_when_target_downstream_ref_is_not_cached() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/site",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={"acme/site": {"main"}},
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/base",
            ref="v1",
            source_key="source:prod",
            direction="both",
            depth=2,
        )
    )

    assert [(edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        ("acme/site@main", "acme/base@v1", "impacts"),
    ]
    assert graph.warnings == (
        "not expanding acme/base@v1 from cached source data: repo/ref is not cached. "
        "Add it to the source, scan the matching ref/tag, or use --live for downstream.",
    )


def test_upstream_graph_keeps_multiple_matching_refs_from_same_repo() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/playbook",
                source_ref="master",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v3",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/playbook",
                source_ref="v3",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v3",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={"acme/playbook": {"master", "v3"}},
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/base",
            ref="v3",
            source_key="source:prod",
            direction="impact",
            depth=1,
        )
    )

    assert [(edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        ("acme/playbook@master", "acme/base@v3", "impacts"),
        ("acme/playbook@v3", "acme/base@v3", "impacts"),
    ]


def test_upstream_without_ref_keeps_each_matching_target_ref() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/site",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/site",
                source_ref="release",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={"acme/site": {"main", "release"}},
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/base",
            ref=None,
            source_key="source:prod",
            direction="impact",
            depth=1,
        )
    )

    assert graph.target_id == "acme/base"
    assert [(edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        ("acme/site@main", "acme/base@main", "impacts"),
        ("acme/site@release", "acme/base@v1", "impacts"),
    ]


def test_graph_traversal_caches_repeated_index_reads_for_converging_paths() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/root",
                source_ref="main",
                dependency_repo="acme/left",
                dependency_name="left",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/root",
                source_ref="main",
                dependency_repo="acme/right",
                dependency_name="right",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/left",
                source_ref="main",
                dependency_repo="acme/shared",
                dependency_name="shared",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/right",
                source_ref="main",
                dependency_repo="acme/shared",
                dependency_name="shared",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/shared",
                source_ref="main",
                dependency_repo="acme/leaf",
                dependency_name="leaf",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={
            "acme/root": {"main"},
            "acme/left": {"main"},
            "acme/right": {"main"},
            "acme/shared": {"main"},
            "acme/leaf": {"main"},
        },
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/root",
            ref="main",
            source_key="source:prod",
            direction="deps",
            depth=4,
        )
    )

    assert ("acme/shared@main", "acme/leaf@main", "requires") in [
        (edge.source_id, edge.target_id, edge.relation) for edge in graph.edges
    ]
    assert index.dependency_calls[("acme/shared", "main", "source:prod")] == 1


def test_impact_traversal_caches_repeated_index_reads_for_converging_paths() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/left",
                source_ref="main",
                dependency_repo="acme/root",
                dependency_name="root",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/right",
                source_ref="main",
                dependency_repo="acme/root",
                dependency_name="root",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/shared",
                source_ref="main",
                dependency_repo="acme/left",
                dependency_name="left",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/shared",
                source_ref="main",
                dependency_repo="acme/right",
                dependency_name="right",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/leaf",
                source_ref="main",
                dependency_repo="acme/shared",
                dependency_name="shared",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_refs={
            "acme/root": {"main"},
            "acme/left": {"main"},
            "acme/right": {"main"},
            "acme/shared": {"main"},
            "acme/leaf": {"main"},
        },
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/root",
            ref="main",
            source_key="source:prod",
            direction="impact",
            depth=4,
        )
    )

    assert ("acme/leaf@main", "acme/shared@main", "impacts") in [
        (edge.source_id, edge.target_id, edge.relation) for edge in graph.edges
    ]
    assert index.dependent_calls[("acme/shared", "main", "source:prod")] == 1
