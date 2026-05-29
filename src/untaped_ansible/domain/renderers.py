"""Render dependency graphs for CLI and documentation output."""

from __future__ import annotations

import json
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
    deps = [nodes[edge.target_id].label for edge in graph.edges if edge.relation == "requires"]
    impacts = [nodes[edge.source_id].label for edge in graph.edges if edge.relation == "impacts"]
    if deps:
        lines.append("+-- deps")
        lines.extend(f"|   +-- {label}" for label in deps)
    if impacts:
        lines.append("+-- impact")
        lines.extend(f"    +-- {label}" for label in impacts)
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


def _mermaid_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in value)
    if cleaned and not cleaned[0].isdigit():
        return cleaned
    return f"n_{cleaned}"


def _escape_mermaid(value: str) -> str:
    return value.replace('"', '\\"')


def _escape_mermaid_comment(value: str) -> str:
    return value.replace("\n", " ")
