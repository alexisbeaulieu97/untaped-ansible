"""Cycle detection for emitted dependency graphs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise

from untaped_ansible.domain.graph import EdgeRelation, GraphCycle, GraphEdge

MAX_CYCLE_COMPONENT_NODES = 50
MAX_CYCLE_COMPONENT_EDGES = 200


def detect_cycles(
    edges: Sequence[GraphEdge],
) -> tuple[tuple[GraphCycle, ...], tuple[str, ...]]:
    """Find canonical simple cycles in the emitted graph edges."""
    cycles: set[GraphCycle] = set()
    warnings: list[str] = []
    for relation in ("requires", "impacts"):
        relation_edges = [edge for edge in edges if edge.relation == relation]
        relation_cycles, relation_warnings = _detect_relation_cycles(relation, relation_edges)
        cycles.update(relation_cycles)
        warnings.extend(relation_warnings)
    return tuple(sorted(cycles, key=_cycle_key)), tuple(warnings)


def _detect_relation_cycles(
    relation: EdgeRelation,
    edges: Sequence[GraphEdge],
) -> tuple[set[GraphCycle], list[str]]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    edge_ids: dict[tuple[str, str], str] = {}
    nodes: set[str] = set()
    for edge in edges:
        adjacency[edge.source_id].append(edge.target_id)
        edge_ids[(edge.source_id, edge.target_id)] = edge.id
        nodes.add(edge.source_id)
        nodes.add(edge.target_id)
    for source_id in adjacency:
        adjacency[source_id] = sorted(set(adjacency[source_id]))

    cycles: set[GraphCycle] = set()
    warnings: list[str] = []
    for component in _strongly_connected_components(nodes, adjacency):
        component_edges = [
            (source_id, target_id)
            for source_id in component
            for target_id in adjacency.get(source_id, ())
            if target_id in component
        ]
        if len(component) == 1 and not any(source == target for source, target in component_edges):
            continue
        if (
            len(component) > MAX_CYCLE_COMPONENT_NODES
            or len(component_edges) > MAX_CYCLE_COMPONENT_EDGES
        ):
            warnings.append(
                f"cycle enumeration skipped for {relation} component with "
                f"{len(component)} nodes and {len(component_edges)} edges"
            )
            continue
        component_cycles = _simple_cycles(sorted(component), adjacency)
        for node_ids in component_cycles:
            closed = (*node_ids, node_ids[0])
            cycle_edge_ids = tuple(
                edge_ids[(source_id, target_id)] for source_id, target_id in pairwise(closed)
            )
            cycles.add(
                GraphCycle(
                    direction=relation,
                    node_ids=closed,
                    edge_ids=cycle_edge_ids,
                )
            )
    return cycles, warnings


def _strongly_connected_components(
    nodes: set[str],
    adjacency: dict[str, list[str]],
) -> list[set[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in adjacency.get(node, ()):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] != indexes[node]:
            return
        component: set[str] = set()
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        components.append(component)

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return components


def _simple_cycles(
    nodes: list[str],
    adjacency: dict[str, list[str]],
) -> set[tuple[str, ...]]:
    cycles: set[tuple[str, ...]] = set()
    remaining = list(nodes)
    while remaining:
        subgraph_nodes = set(remaining)
        subgraph_adjacency = _subgraph_adjacency(subgraph_nodes, adjacency)
        components = [
            component
            for component in _strongly_connected_components(subgraph_nodes, subgraph_adjacency)
            if _has_cycle(component, subgraph_adjacency)
        ]
        if not components:
            break
        component = min(components, key=lambda item: min(item))
        start = min(component)
        _johnson_circuits(start, component, subgraph_adjacency, cycles)
        remaining = [node for node in remaining if node != start]
    return cycles


def _subgraph_adjacency(
    nodes: set[str],
    adjacency: dict[str, list[str]],
) -> dict[str, list[str]]:
    return {
        node: [target for target in adjacency.get(node, ()) if target in nodes] for node in nodes
    }


def _has_cycle(component: set[str], adjacency: dict[str, list[str]]) -> bool:
    if len(component) > 1:
        return True
    node = next(iter(component))
    return node in adjacency.get(node, ())


def _johnson_circuits(
    start: str,
    component: set[str],
    adjacency: dict[str, list[str]],
    cycles: set[tuple[str, ...]],
) -> None:
    blocked: set[str] = set()
    blocked_by: dict[str, set[str]] = defaultdict(set)
    stack: list[str] = []

    def unblock(node: str) -> None:
        blocked.discard(node)
        while blocked_by[node]:
            blocker = blocked_by[node].pop()
            if blocker in blocked:
                unblock(blocker)

    def circuit(node: str) -> bool:
        found_cycle = False
        stack.append(node)
        blocked.add(node)
        for target in adjacency.get(node, ()):
            if target not in component:
                continue
            if target == start:
                cycles.add(_canonical_cycle(tuple(stack)))
                found_cycle = True
            elif target not in blocked and circuit(target):
                found_cycle = True
        if found_cycle:
            unblock(node)
        else:
            for target in adjacency.get(node, ()):
                if target in component:
                    blocked_by[target].add(node)
        stack.pop()
        return found_cycle

    circuit(start)


def _canonical_cycle(node_ids: tuple[str, ...]) -> tuple[str, ...]:
    rotations = [node_ids[index:] + node_ids[:index] for index in range(len(node_ids))]
    return min(rotations)


def _cycle_key(cycle: GraphCycle) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return cycle.direction, cycle.node_ids, cycle.edge_ids
