"""Cycle detection for emitted dependency graphs."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from itertools import pairwise
from typing import get_args

from untaped_ansible.domain.graph import EdgeRelation, GraphCycle, GraphEdge

MAX_CYCLES_PER_COMPONENT = 100


def detect_cycles(
    edges: Sequence[GraphEdge],
) -> tuple[tuple[GraphCycle, ...], tuple[str, ...]]:
    """Find deterministic cycle records in the emitted graph edges."""
    cycles: list[GraphCycle] = []
    warnings: list[str] = []
    for relation in get_args(EdgeRelation):
        relation_edges = [edge for edge in edges if edge.relation == relation]
        relation_cycles, relation_warnings = _detect_relation_cycles(relation, relation_edges)
        cycles.extend(relation_cycles)
        warnings.extend(relation_warnings)
    return tuple(sorted(cycles, key=_cycle_key)), tuple(warnings)


def _detect_relation_cycles(
    relation: EdgeRelation,
    edges: Sequence[GraphEdge],
) -> tuple[list[GraphCycle], list[str]]:
    adjacency: dict[str, list[str]] = {}
    edge_ids: dict[tuple[str, str], str] = {}
    nodes: set[str] = set()
    for edge in edges:
        adjacency.setdefault(edge.source_id, []).append(edge.target_id)
        edge_ids[(edge.source_id, edge.target_id)] = edge.id
        nodes.add(edge.source_id)
        nodes.add(edge.target_id)
    for source_id in adjacency:
        adjacency[source_id] = sorted(set(adjacency[source_id]))

    cycles: list[GraphCycle] = []
    warnings: list[str] = []
    for component in _strongly_connected_components(nodes, adjacency):
        component_edges = _component_edges(component, adjacency)
        if len(component) == 1 and not any(source == target for source, target in component_edges):
            continue

        component_cycles, overflow = _bounded_simple_cycles(
            component,
            adjacency,
            limit=MAX_CYCLES_PER_COMPONENT,
        )
        if overflow:
            cycles.append(
                GraphCycle(
                    kind="scc_group",
                    relation=relation,
                    node_ids=tuple(sorted(component)),
                    edge_ids=tuple(sorted(edge_ids[edge] for edge in component_edges)),
                )
            )
            warnings.append(
                f"cycle output for {relation} component starting at {min(component)} "
                f"exceeded {MAX_CYCLES_PER_COMPONENT} cycles; reported "
                f"{len(component)}-node/{len(component_edges)}-edge SCC group instead"
            )
            continue

        for node_ids in component_cycles:
            closed = (*node_ids, node_ids[0])
            cycle_edge_ids = tuple(
                edge_ids[(source_id, target_id)] for source_id, target_id in pairwise(closed)
            )
            cycles.append(
                GraphCycle(
                    kind="cycle",
                    relation=relation,
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

    def push(node: str, frames: list[tuple[str, Iterator[str]]]) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        frames.append((node, iter(adjacency.get(node, ()))))

    for start in sorted(nodes):
        if start in indexes:
            continue
        frames: list[tuple[str, Iterator[str]]] = []
        push(start, frames)
        while frames:
            node, targets = frames[-1]
            try:
                target = next(targets)
            except StopIteration:
                _finish_component_frame(
                    node,
                    frames=frames,
                    stack=stack,
                    on_stack=on_stack,
                    indexes=indexes,
                    lowlinks=lowlinks,
                    components=components,
                )
                continue
            if target not in nodes:
                continue
            if target not in indexes:
                push(target, frames)
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
    return components


def _finish_component_frame(
    node: str,
    *,
    frames: list[tuple[str, Iterator[str]]],
    stack: list[str],
    on_stack: set[str],
    indexes: dict[str, int],
    lowlinks: dict[str, int],
    components: list[set[str]],
) -> None:
    frames.pop()
    if lowlinks[node] == indexes[node]:
        components.append(_pop_component(node, stack, on_stack))
    if frames:
        parent = frames[-1][0]
        lowlinks[parent] = min(lowlinks[parent], lowlinks[node])


def _pop_component(root: str, stack: list[str], on_stack: set[str]) -> set[str]:
    component: set[str] = set()
    while True:
        member = stack.pop()
        on_stack.remove(member)
        component.add(member)
        if member == root:
            return component


def _component_edges(
    component: set[str],
    adjacency: dict[str, list[str]],
) -> list[tuple[str, str]]:
    return [
        (source_id, target_id)
        for source_id in sorted(component)
        for target_id in adjacency.get(source_id, ())
        if target_id in component
    ]


def _bounded_simple_cycles(
    component: set[str],
    adjacency: dict[str, list[str]],
    *,
    limit: int,
) -> tuple[list[tuple[str, ...]], bool]:
    cycles: list[tuple[str, ...]] = []
    remaining = set(component)
    while remaining:
        subgraph_adjacency = _subgraph_adjacency(remaining, adjacency)
        components = [
            candidate
            for candidate in _strongly_connected_components(remaining, subgraph_adjacency)
            if _has_cycle(candidate, subgraph_adjacency)
        ]
        if not components:
            break
        active = min(components, key=lambda candidate: min(candidate))
        start = min(active)
        component_cycles, overflow = _johnson_component_cycles(
            start,
            active,
            subgraph_adjacency,
            limit=limit - len(cycles),
        )
        cycles.extend(component_cycles)
        if overflow:
            return cycles, True
        remaining.remove(start)
    return cycles, False


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


def _johnson_component_cycles(
    start: str,
    component: set[str],
    adjacency: dict[str, list[str]],
    *,
    limit: int,
) -> tuple[list[tuple[str, ...]], bool]:
    cycles: list[tuple[str, ...]] = []
    blocked = {start}
    blocked_by: dict[str, set[str]] = {node: set() for node in component}
    closed: set[str] = set()
    path = [start]
    frames: list[tuple[str, Iterator[str]]] = [
        (start, iter(_component_targets(start, component, adjacency)))
    ]
    while frames:
        node, targets = frames[-1]
        try:
            target = next(targets)
        except StopIteration:
            if node in closed:
                _unblock(node, blocked, blocked_by)
            else:
                for target in _component_targets(node, component, adjacency):
                    blocked_by[target].add(node)
            frames.pop()
            path.pop()
            continue

        if target == start:
            cycles.append(tuple(path))
            if len(cycles) > limit:
                return cycles, True
            closed.update(path)
        elif target not in blocked:
            path.append(target)
            closed.discard(target)
            blocked.add(target)
            frames.append((target, iter(_component_targets(target, component, adjacency))))
    return cycles, False


def _component_targets(
    node: str,
    component: set[str],
    adjacency: dict[str, list[str]],
) -> Iterator[str]:
    for target in adjacency.get(node, ()):
        if target in component:
            yield target


def _unblock(
    node: str,
    blocked: set[str],
    blocked_by: dict[str, set[str]],
) -> None:
    pending = [node]
    while pending:
        current = pending.pop()
        if current not in blocked:
            continue
        blocked.remove(current)
        pending.extend(blocked_by[current])
        blocked_by[current].clear()


def _cycle_key(cycle: GraphCycle) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    return cycle.kind, cycle.relation, cycle.node_ids, cycle.edge_ids
