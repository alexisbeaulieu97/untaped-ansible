"""Tests for dependency graph renderers."""

from __future__ import annotations

import json

from untaped_ansible.domain.graph import DependencyGraph, GraphEdge, GraphNode
from untaped_ansible.domain.renderers import render_graph


def _graph() -> DependencyGraph:
    return DependencyGraph(
        target_id="target",
        nodes=(
            GraphNode(id="target", label="acme/base@v1.0.0", repo="acme/base", ref="v1.0.0"),
            GraphNode(id="users", label="acme/users@main", repo="acme/users", ref="main"),
            GraphNode(id="site", label="acme/site@release/1", repo="acme/site", ref="release/1"),
            GraphNode(id="missing", label="unresolved: common", unresolved="common"),
        ),
        edges=(
            GraphEdge(source_id="target", target_id="users", relation="requires"),
            GraphEdge(source_id="target", target_id="missing", relation="requires"),
            GraphEdge(source_id="site", target_id="target", relation="impacts"),
        ),
        warnings=("source data is stale",),
    )


def test_tree_renderer_groups_dependencies_and_impact() -> None:
    rendered = render_graph(_graph(), "tree")

    assert "acme/base@v1.0.0" in rendered
    assert "+-- downstream" in rendered
    assert "|   +-- acme/base@v1.0.0" in rendered
    assert "|       +-- acme/users@main" in rendered
    assert "|       +-- unresolved: common" in rendered
    assert "+-- upstream" in rendered
    assert "    +-- acme/base@v1.0.0" in rendered
    assert "        +-- acme/site@release/1" in rendered
    assert "warning: source data is stale" in rendered


def test_tree_renderer_nests_transitive_downstream_paths() -> None:
    graph = DependencyGraph(
        target_id="target",
        nodes=(
            GraphNode(id="target", label="acme/base@v1", repo="acme/base", ref="v1"),
            GraphNode(id="users", label="acme/users@main", repo="acme/users", ref="main"),
            GraphNode(id="common", label="acme/common@main", repo="acme/common", ref="main"),
        ),
        edges=(
            GraphEdge(source_id="target", target_id="users", relation="requires"),
            GraphEdge(source_id="users", target_id="common", relation="requires"),
        ),
    )

    rendered = render_graph(graph, "tree")

    assert "|   +-- acme/base@v1" in rendered
    assert "|       +-- acme/users@main" in rendered
    assert "|           +-- acme/common@main" in rendered


def test_tree_renderer_renders_same_node_in_upstream_and_downstream_sections() -> None:
    graph = DependencyGraph(
        target_id="target",
        nodes=(
            GraphNode(id="target", label="acme/base@v1", repo="acme/base", ref="v1"),
            GraphNode(id="shared", label="acme/shared@main", repo="acme/shared", ref="main"),
        ),
        edges=(
            GraphEdge(source_id="target", target_id="shared", relation="requires"),
            GraphEdge(source_id="shared", target_id="target", relation="impacts"),
        ),
    )

    rendered = render_graph(graph, "tree")

    assert "|   +-- acme/base@v1" in rendered
    assert "|       +-- acme/shared@main" in rendered
    assert "    +-- acme/base@v1" in rendered
    assert "        +-- acme/shared@main" in rendered


def test_tree_renderer_renders_multiple_upstream_refs_from_same_repo() -> None:
    graph = DependencyGraph(
        target_id="target",
        nodes=(
            GraphNode(id="target", label="acme/base@v3", repo="acme/base", ref="v3"),
            GraphNode(
                id="playbook-master",
                label="acme/playbook@master",
                repo="acme/playbook",
                ref="master",
            ),
            GraphNode(
                id="playbook-v3",
                label="acme/playbook@v3",
                repo="acme/playbook",
                ref="v3",
            ),
        ),
        edges=(
            GraphEdge(source_id="playbook-master", target_id="target", relation="impacts"),
            GraphEdge(source_id="playbook-v3", target_id="target", relation="impacts"),
        ),
    )

    rendered = render_graph(graph, "tree")

    assert "    +-- acme/base@v3" in rendered
    assert "        +-- acme/playbook@master" in rendered
    assert "        +-- acme/playbook@v3" in rendered


def test_tree_renderer_renders_multiple_target_refs_under_each_direction() -> None:
    graph = DependencyGraph(
        target_id="target",
        nodes=(
            GraphNode(id="target", label="acme/base", repo="acme/base"),
            GraphNode(
                id="target-main",
                label="acme/base@main",
                repo="acme/base",
                ref="main",
            ),
            GraphNode(id="target-v1", label="acme/base@v1", repo="acme/base", ref="v1"),
            GraphNode(id="users", label="acme/users@v1", repo="acme/users", ref="v1"),
            GraphNode(id="legacy", label="acme/legacy@v1", repo="acme/legacy", ref="v1"),
            GraphNode(id="site-main", label="acme/site@main", repo="acme/site", ref="main"),
            GraphNode(
                id="site-release",
                label="acme/site@release",
                repo="acme/site",
                ref="release",
            ),
        ),
        edges=(
            GraphEdge(source_id="target-main", target_id="users", relation="requires"),
            GraphEdge(source_id="target-v1", target_id="legacy", relation="requires"),
            GraphEdge(source_id="site-main", target_id="target-main", relation="impacts"),
            GraphEdge(source_id="site-release", target_id="target-v1", relation="impacts"),
        ),
    )

    rendered = render_graph(graph, "tree")

    assert "|   +-- acme/base@main" in rendered
    assert "|       +-- acme/users@v1" in rendered
    assert "|   +-- acme/base@v1" in rendered
    assert "|       +-- acme/legacy@v1" in rendered
    assert "    +-- acme/base@main" in rendered
    assert "        +-- acme/site@main" in rendered
    assert "    +-- acme/base@v1" in rendered
    assert "        +-- acme/site@release" in rendered


def test_mermaid_renderer_emits_directional_edges() -> None:
    rendered = render_graph(_graph(), "mermaid")

    assert rendered.startswith("graph LR\n")
    assert 'target["acme/base@v1.0.0"]' in rendered
    assert "target --> users" in rendered
    assert "site --> target" in rendered
    assert "target --> missing" in rendered
    assert "%% warning: source data is stale" in rendered


def test_json_renderer_emits_structured_graph() -> None:
    data = json.loads(render_graph(_graph(), "json"))

    assert data["target_id"] == "target"
    assert data["nodes"][0]["repo"] == "acme/base"
    assert data["edges"][0]["relation"] == "requires"
    assert data["warnings"] == ["source data is stale"]
