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
    downstream_roots = _target_roots(downstream, nodes, target)
    if downstream_roots:
        lines.append("+-- downstream")
        _append_tree_roots(
            lines,
            nodes=nodes,
            adjacency=downstream,
            root_ids=downstream_roots,
            prefix="|   ",
            target_id=graph.target_id,
        )
    upstream_roots = _target_roots(upstream, nodes, target)
    if upstream_roots:
        lines.append("+-- upstream")
        _append_tree_roots(
            lines,
            nodes=nodes,
            adjacency=upstream,
            root_ids=upstream_roots,
            prefix="    ",
            target_id=graph.target_id,
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


def _target_roots(
    adjacency: dict[str, list[str]],
    nodes: dict[str, GraphNode],
    target: GraphNode,
) -> list[str]:
    roots: list[str] = []
    for parent_id in adjacency:
        node = nodes[parent_id]
        if parent_id == target.id or _is_concrete_target_ref(node, target):
            roots.append(parent_id)
    return roots


def _is_concrete_target_ref(node: GraphNode, target: GraphNode) -> bool:
    return (
        target.ref is None
        and target.repo is not None
        and node.repo == target.repo
        and node.ref is not None
    )


def _append_tree_roots(
    lines: list[str],
    *,
    nodes: dict[str, GraphNode],
    adjacency: dict[str, list[str]],
    root_ids: list[str],
    prefix: str,
    target_id: str,
) -> None:
    for root_id in root_ids:
        root = nodes[root_id]
        lines.append(f"{prefix}+-- {root.label}")
        _append_tree_children(
            lines,
            nodes=nodes,
            adjacency=adjacency,
            parent_id=root_id,
            prefix=f"{prefix}    ",
            path={target_id, root_id},
        )


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
