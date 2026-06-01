"""Render dependency graphs for CLI and documentation output."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from untaped_ansible.domain.graph import DependencyGraph, GraphNode

GraphFormat = Literal["tree", "mermaid", "json"]


def render_graph(graph: DependencyGraph, fmt: GraphFormat) -> str:
    if fmt == "tree":
        return _render_tree(graph)
    if fmt == "mermaid":
        return _render_mermaid(graph)
    if fmt == "json":
        return json.dumps(graph.model_dump(), default=str)
    raise ValueError(f"unknown graph format: {fmt!r}")


def _render_tree(graph: DependencyGraph) -> str:
    nodes = _nodes_by_id(graph)
    target = nodes[graph.target_id]
    lines = [target.label]
    downstream = _adjacency(
        (edge.source_id, edge.target_id) for edge in graph.edges if edge.relation == "requires"
    )
    upstream = _adjacency(
        (edge.target_id, edge.source_id) for edge in graph.edges if edge.relation == "impacts"
    )
    if downstream.get(graph.target_id):
        lines.append("+-- downstream")
        _append_tree_children(
            lines,
            nodes=nodes,
            adjacency=downstream,
            parent_id=graph.target_id,
            prefix="|   ",
            path={graph.target_id},
        )
    if upstream.get(graph.target_id):
        lines.append("+-- upstream")
        _append_tree_children(
            lines,
            nodes=nodes,
            adjacency=upstream,
            parent_id=graph.target_id,
            prefix="    ",
            path={graph.target_id},
        )
    lines.extend(f"warning: {warning}" for warning in graph.warnings)
    return "\n".join(lines)


def _render_mermaid(graph: DependencyGraph) -> str:
    lines = ["graph LR"]
    for node in graph.nodes:
        lines.append(f'  {_mermaid_id(node.id)}["{_escape_mermaid(node.label)}"]')
    for edge in graph.edges:
        lines.append(f"  {_mermaid_id(edge.source_id)} --> {_mermaid_id(edge.target_id)}")
    lines.extend(f"  %% warning: {_escape_mermaid_comment(warning)}" for warning in graph.warnings)
    return "\n".join(lines)


def _nodes_by_id(graph: DependencyGraph) -> dict[str, GraphNode]:
    return {node.id: node for node in graph.nodes}


def _adjacency(edges: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {}
    for source_id, target_id in edges:
        adjacency.setdefault(source_id, []).append(target_id)
    return adjacency


def _append_tree_children(
    lines: list[str],
    *,
    nodes: dict[str, GraphNode],
    adjacency: dict[str, list[str]],
    parent_id: str,
    prefix: str,
    path: set[str],
) -> None:
    for child_id in adjacency.get(parent_id, []):
        child = nodes[child_id]
        if child_id in path:
            lines.append(f"{prefix}+-- {child.label} (cycle)")
            continue
        lines.append(f"{prefix}+-- {child.label}")
        _append_tree_children(
            lines,
            nodes=nodes,
            adjacency=adjacency,
            parent_id=child_id,
            prefix=f"{prefix}    ",
            path={*path, child_id},
        )


def _mermaid_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in value)
    if cleaned and not cleaned[0].isdigit():
        return cleaned
    return f"n_{cleaned}"


def _escape_mermaid(value: str) -> str:
    return value.replace('"', '\\"')


def _escape_mermaid_comment(value: str) -> str:
    return value.replace("\n", " ")
