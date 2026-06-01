"""Tests for dependency and impact graph construction."""

from __future__ import annotations

from untaped_ansible.application.graph import BuildGraph, GraphRequest
from untaped_ansible.application.ports import IndexedDependency


class StubIndex:
    def __init__(self, edges: list[IndexedDependency], *, stale: bool = False) -> None:
        self.edges = edges
        self.stale = stale

    def dependencies(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        return [edge for edge in self.edges if edge.source_repo == repo and edge.source_ref == ref]

    def dependents(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges
            if edge.dependency_repo == repo and (ref is None or edge.dependency_version == ref)
        ]

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self.stale


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
