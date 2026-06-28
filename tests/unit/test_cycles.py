"""Tests for cycle detection over emitted graph edges."""

from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

import untaped_ansible.domain.cycles as cycles_module
from untaped_ansible.domain.cycles import detect_cycles
from untaped_ansible.domain.graph import GraphCycle, GraphEdge


def _edge_id(relation: str, source_id: str, target_id: str) -> str:
    digest = sha256(f"{relation}\0{source_id}\0{target_id}".encode()).hexdigest()[:16]
    return f"edge:{digest}"


def _edge(source_id: str, target_id: str, relation: str = "requires") -> GraphEdge:
    return GraphEdge(source_id=source_id, target_id=target_id, relation=relation)


def test_overlapping_simple_cycles_are_reported_as_elementary_cycles() -> None:
    graph_cycles, warnings = detect_cycles(
        [
            _edge("a", "b"),
            _edge("b", "a"),
            _edge("a", "c"),
            _edge("c", "a"),
        ]
    )

    assert warnings == ()
    assert [
        (cycle.kind, cycle.relation, cycle.node_ids, cycle.edge_ids) for cycle in graph_cycles
    ] == [
        (
            "cycle",
            "requires",
            ("a", "b", "a"),
            (_edge_id("requires", "a", "b"), _edge_id("requires", "b", "a")),
        ),
        (
            "cycle",
            "requires",
            ("a", "c", "a"),
            (_edge_id("requires", "a", "c"), _edge_id("requires", "c", "a")),
        ),
    ]


def test_cycle_cap_keeps_exactly_capped_cycles(monkeypatch) -> None:
    monkeypatch.setattr(cycles_module, "MAX_CYCLES_PER_COMPONENT", 2)

    graph_cycles, warnings = detect_cycles(
        [
            _edge("a", "b"),
            _edge("b", "a"),
            _edge("a", "c"),
            _edge("c", "a"),
        ]
    )

    assert warnings == ()
    assert [cycle.kind for cycle in graph_cycles] == ["cycle", "cycle"]


def test_cycle_cap_overflow_emits_deterministic_scc_group(monkeypatch) -> None:
    monkeypatch.setattr(cycles_module, "MAX_CYCLES_PER_COMPONENT", 1)
    edges = [
        _edge("a", "b"),
        _edge("b", "a"),
        _edge("a", "c"),
        _edge("c", "a"),
    ]

    first_cycles, first_warnings = detect_cycles(edges)
    second_cycles, second_warnings = detect_cycles(list(reversed(edges)))

    assert [
        (cycle.kind, cycle.relation, cycle.node_ids, cycle.edge_ids) for cycle in first_cycles
    ] == [
        (
            "scc_group",
            "requires",
            ("a", "b", "c"),
            tuple(
                sorted(
                    [
                        _edge_id("requires", "a", "b"),
                        _edge_id("requires", "a", "c"),
                        _edge_id("requires", "b", "a"),
                        _edge_id("requires", "c", "a"),
                    ]
                )
            ),
        )
    ]
    assert first_cycles == second_cycles
    assert (
        first_warnings
        == second_warnings
        == (
            "cycle output for requires component starting at a exceeded 1 cycles; "
            "reported 3-node/4-edge SCC group instead",
        )
    )


def test_large_sparse_cycle_does_not_overflow_recursion_limit() -> None:
    size = 1200
    graph_cycles, warnings = detect_cycles(
        [_edge(f"node-{index:04d}", f"node-{(index + 1) % size:04d}") for index in range(size)]
    )

    assert warnings == ()
    assert len(graph_cycles) == 1
    assert graph_cycles[0].kind == "cycle"
    assert len(graph_cycles[0].node_ids) == size + 1


def test_both_relations_keep_scc_groups_distinct(monkeypatch) -> None:
    monkeypatch.setattr(cycles_module, "MAX_CYCLES_PER_COMPONENT", 1)

    graph_cycles, warnings = detect_cycles(
        [
            _edge("a", "b", "requires"),
            _edge("b", "a", "requires"),
            _edge("a", "c", "requires"),
            _edge("c", "a", "requires"),
            _edge("x", "y", "impacts"),
            _edge("y", "x", "impacts"),
            _edge("x", "z", "impacts"),
            _edge("z", "x", "impacts"),
        ]
    )

    assert [(cycle.kind, cycle.relation, cycle.node_ids) for cycle in graph_cycles] == [
        ("scc_group", "impacts", ("x", "y", "z")),
        ("scc_group", "requires", ("a", "b", "c")),
    ]
    assert warnings == (
        "cycle output for requires component starting at a exceeded 1 cycles; "
        "reported 3-node/4-edge SCC group instead",
        "cycle output for impacts component starting at x exceeded 1 cycles; "
        "reported 3-node/4-edge SCC group instead",
    )


def test_graph_cycle_model_validates_kind_specific_node_shape() -> None:
    GraphCycle(
        kind="cycle",
        relation="requires",
        node_ids=("a", "b", "a"),
        edge_ids=(_edge_id("requires", "a", "b"), _edge_id("requires", "b", "a")),
    )
    GraphCycle(
        kind="scc_group",
        relation="requires",
        node_ids=("a", "b"),
        edge_ids=tuple(sorted((_edge_id("requires", "a", "b"), _edge_id("requires", "b", "a")))),
    )

    with pytest.raises(ValidationError):
        GraphCycle(
            kind="cycle",
            relation="requires",
            node_ids=("a", "b"),
            edge_ids=(_edge_id("requires", "a", "b"),),
        )
    with pytest.raises(ValidationError):
        GraphCycle(
            kind="scc_group",
            relation="requires",
            node_ids=("b", "a"),
            edge_ids=tuple(
                sorted((_edge_id("requires", "a", "b"), _edge_id("requires", "b", "a")))
            ),
        )
    with pytest.raises(ValidationError):
        GraphCycle(
            kind="scc_group",
            relation="requires",
            node_ids=("a", "b"),
            edge_ids=(_edge_id("requires", "a", "b"), _edge_id("requires", "b", "a")),
        )
