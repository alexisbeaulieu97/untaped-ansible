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
    assert "|   +-- acme/users@main" in rendered
    assert "|   +-- unresolved: common" in rendered
    assert "+-- upstream" in rendered
    assert "    +-- acme/site@release/1" in rendered
    assert "warning: source data is stale" in rendered


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
