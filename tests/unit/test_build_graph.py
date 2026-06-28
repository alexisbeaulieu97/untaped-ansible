"""Tests for dependency and impact graph construction."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from hashlib import sha256

from untaped_ansible.application.graph import BuildGraph, GraphRequest
from untaped_ansible.domain.payloads import CachedRef, IndexedDependency
from untaped_ansible.domain.renderers import render_graph


def _edge_id(relation: str, source_id: str, target_id: str) -> str:
    digest = sha256(f"{relation}\0{source_id}\0{target_id}".encode()).hexdigest()[:16]
    return f"edge:{digest}"


def _chain_edges(length: int, *, cycle: bool = False) -> list[IndexedDependency]:
    edges: list[IndexedDependency] = []
    limit = length if cycle else length - 1
    for index in range(limit):
        source = f"acme/role-{index:04d}"
        target = f"acme/role-{(index + 1) % length:04d}"
        edges.append(
            IndexedDependency(
                source_repo=source,
                source_ref="main",
                dependency_repo=target,
                dependency_name=target.rsplit("/", maxsplit=1)[-1],
                dependency_version="main",
                source_path="roles/requirements.yml",
            )
        )
    return edges


class StubIndex:
    def __init__(
        self,
        edges: list[IndexedDependency],
        *,
        cached_refs: dict[str, set[str]] | None = None,
        cached_ref_metadata: dict[str, tuple[CachedRef, ...]] | None = None,
        stale: bool = False,
    ) -> None:
        self.edges = edges
        self.stale = stale
        self._cached_ref_metadata = cached_ref_metadata or {}
        if cached_refs is None:
            cached_refs = {}
            for edge in edges:
                if edge.source_ref is not None:
                    cached_refs.setdefault(edge.source_repo, set()).add(edge.source_ref)
        self._cached_refs = cached_refs
        # Point-read counters, kept separate from the batch-call records below
        # so tests can prove the traversal never falls back to point reads.
        self.dependency_calls: dict[tuple[str, str | None, str | None], int] = {}
        self.dependent_calls: dict[tuple[str, str | None, str | None], int] = {}
        self.cached_refs_calls: dict[tuple[str, str | None], int] = {}
        # Batch-call records: one entry per batch read, listing the pairs asked.
        self.dependencies_batch_calls: list[list[tuple[str, str | None]]] = []
        self.dependents_batch_calls: list[list[tuple[str, str | None]]] = []

    def _dependency_edges(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges
            if edge.source_repo == repo and (ref is None or edge.source_ref == ref)
        ]

    def _dependent_edges(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        return [
            edge
            for edge in self.edges
            if edge.dependency_repo == repo and (ref is None or edge.dependency_version == ref)
        ]

    def dependencies(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        key = (repo, ref, source_key)
        self.dependency_calls[key] = self.dependency_calls.get(key, 0) + 1
        return self._dependency_edges(repo, ref)

    def dependents(
        self, repo: str, ref: str | None, *, source_key: str | None
    ) -> list[IndexedDependency]:
        key = (repo, ref, source_key)
        self.dependent_calls[key] = self.dependent_calls.get(key, 0) + 1
        return self._dependent_edges(repo, ref)

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self.stale

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        key = (repo, source_key)
        self.cached_refs_calls[key] = self.cached_refs_calls.get(key, 0) + 1
        if source_key is None:
            return set()
        return set(self._cached_refs.get(repo, set()))

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        if source_key is None:
            return ()
        return self._cached_ref_metadata.get(repo, ())

    def dependencies_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        # Answers from stub data directly so point-read counters stay untouched.
        self.dependencies_batch_calls.append(list(pairs))
        return {(repo, ref): self._dependency_edges(repo, ref) for repo, ref in pairs}

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        # Answers from stub data directly so point-read counters stay untouched.
        self.dependents_batch_calls.append(list(pairs))
        return {(repo, ref): self._dependent_edges(repo, ref) for repo, ref in pairs}

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[CachedRef, ...]]:
        return {repo: self.cached_ref_metadata(repo, source_key=source_key) for repo in repos}


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


def test_downstream_cycle_emits_closing_edge_and_structured_cycle() -> None:
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

    assert [(edge.id, edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        (
            _edge_id("requires", "acme/base@v1", "acme/users@main"),
            "acme/base@v1",
            "acme/users@main",
            "requires",
        ),
        (
            _edge_id("requires", "acme/users@main", "acme/base@v1"),
            "acme/users@main",
            "acme/base@v1",
            "requires",
        ),
    ]
    assert [
        (cycle.kind, cycle.relation, cycle.node_ids, cycle.edge_ids) for cycle in graph.cycles
    ] == [
        (
            "cycle",
            "requires",
            ("acme/base@v1", "acme/users@main", "acme/base@v1"),
            (
                _edge_id("requires", "acme/base@v1", "acme/users@main"),
                _edge_id("requires", "acme/users@main", "acme/base@v1"),
            ),
        )
    ]


def test_upstream_cycle_emits_closing_edge_and_structured_cycle() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/users",
                source_ref="main",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v1",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/base",
                source_ref="v1",
                dependency_repo="acme/users",
                dependency_name="users",
                dependency_version="main",
                source_path="meta/main.yml",
            ),
        ]
    )

    graph = BuildGraph(index)(GraphRequest(repo="acme/base", ref="v1", direction="impact", depth=3))

    assert [(edge.id, edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        (
            _edge_id("impacts", "acme/users@main", "acme/base@v1"),
            "acme/users@main",
            "acme/base@v1",
            "impacts",
        ),
        (
            _edge_id("impacts", "acme/base@v1", "acme/users@main"),
            "acme/base@v1",
            "acme/users@main",
            "impacts",
        ),
    ]
    assert [
        (cycle.kind, cycle.relation, cycle.node_ids, cycle.edge_ids) for cycle in graph.cycles
    ] == [
        (
            "cycle",
            "impacts",
            ("acme/base@v1", "acme/users@main", "acme/base@v1"),
            (
                _edge_id("impacts", "acme/base@v1", "acme/users@main"),
                _edge_id("impacts", "acme/users@main", "acme/base@v1"),
            ),
        )
    ]


def test_self_loop_is_reported_as_one_node_cycle() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/base",
                source_ref="v1",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v1",
                source_path="meta/main.yml",
            ),
        ]
    )

    graph = BuildGraph(index)(GraphRequest(repo="acme/base", ref="v1", direction="deps", depth=3))

    assert [(edge.source_id, edge.target_id, edge.relation) for edge in graph.edges] == [
        ("acme/base@v1", "acme/base@v1", "requires")
    ]
    assert [
        (cycle.kind, cycle.relation, cycle.node_ids, cycle.edge_ids) for cycle in graph.cycles
    ] == [
        (
            "cycle",
            "requires",
            ("acme/base@v1", "acme/base@v1"),
            (_edge_id("requires", "acme/base@v1", "acme/base@v1"),),
        )
    ]


def test_cycles_beyond_depth_are_not_reported() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/a",
                source_ref="main",
                dependency_repo="acme/b",
                dependency_name="b",
                dependency_version="main",
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
            IndexedDependency(
                source_repo="acme/c",
                source_ref="main",
                dependency_repo="acme/a",
                dependency_name="a",
                dependency_version="main",
                source_path="roles/requirements.yml",
            ),
        ]
    )

    graph = BuildGraph(index)(GraphRequest(repo="acme/a", ref="main", direction="deps", depth=2))

    assert [(edge.source_id, edge.target_id) for edge in graph.edges] == [
        ("acme/a@main", "acme/b@main"),
        ("acme/b@main", "acme/c@main"),
    ]
    assert graph.cycles == ()


def test_long_acyclic_chain_builds_and_renders_without_recursion_error() -> None:
    length = sys.getrecursionlimit() + 25
    index = StubIndex(_chain_edges(length))

    graph = BuildGraph(index)(
        GraphRequest(repo="acme/role-0000", ref="main", direction="deps", depth=None)
    )
    rendered = render_graph(graph, "tree")

    assert graph.cycles == ()
    assert "acme/role-0000@main" in rendered
    assert f"acme/role-{length - 1:04d}@main" in rendered


def test_long_cyclic_ring_builds_detects_and_renders_without_recursion_error() -> None:
    length = sys.getrecursionlimit() + 25
    index = StubIndex(_chain_edges(length, cycle=True))

    graph = BuildGraph(index)(
        GraphRequest(repo="acme/role-0000", ref="main", direction="deps", depth=None)
    )
    rendered = render_graph(graph, "tree")

    assert len(graph.cycles) == 1
    assert graph.cycles[0].kind == "cycle"
    assert graph.cycles[0].relation == "requires"
    assert len(graph.cycles[0].node_ids) == length + 1
    assert "(cycle)" in rendered


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


def test_refresh_hint_is_appended_to_stale_and_missing_ref_warnings() -> None:
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
        ],
        cached_refs={"acme/a": {"main"}, "acme/b": {"main"}},
        stale=True,
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/a",
            ref="main",
            source_key="source:prod",
            direction="both",
            depth=3,
            refresh_hint="Run `untaped-ansible source refresh prod` to update it.",
        )
    )

    assert graph.warnings == (
        "source data is stale; refresh it before relying on upstream impact. "
        "Run `untaped-ansible source refresh prod` to update it.",
        "not expanding acme/b@v1 from cached source data: ref is not cached "
        "(available refs: main). Scan the matching ref/tag or use --live for downstream. "
        "Run `untaped-ansible source refresh prod` to update it.",
    )


def test_cached_ref_warning_uses_branch_and_semver_display_order() -> None:
    index = StubIndex(
        [],
        cached_refs={"acme/site": {"v1.0.0", "trunk", "v2.0.0", "feature/2", "docs"}},
        cached_ref_metadata={
            "acme/site": (
                CachedRef(name="v1.0.0", kind="tags", default_branch="trunk"),
                CachedRef(name="trunk", kind="heads", default_branch="trunk"),
                CachedRef(name="v2.0.0", kind="tags", default_branch="trunk"),
                CachedRef(name="feature/2", kind="heads", default_branch="trunk"),
                CachedRef(name="docs", default_branch="trunk"),
            )
        },
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/site",
            ref="missing",
            source_key="source:prod",
            direction="deps",
            depth=1,
        )
    )

    assert graph.warnings == (
        "not expanding acme/site@missing from cached source data: ref is not cached "
        "(available refs: trunk, feature/2, v2.0.0, v1.0.0, docs). Scan the matching "
        "ref/tag or use --live for downstream.",
    )


def test_build_graph_attaches_ref_kind_and_default_branch_to_nodes() -> None:
    index = StubIndex(
        [
            IndexedDependency(
                source_repo="acme/site",
                source_ref="trunk",
                source_ref_kind="heads",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="v2.0.0",
                source_path="roles/requirements.yml",
            ),
            IndexedDependency(
                source_repo="acme/site",
                source_ref="v2.0.0",
                source_ref_kind="tags",
                dependency_repo="acme/base",
                dependency_name="base",
                dependency_version="trunk",
                source_path="roles/requirements.yml",
            ),
        ],
        cached_ref_metadata={
            "acme/site": (
                CachedRef(name="trunk", kind="heads", default_branch="trunk"),
                CachedRef(name="v2.0.0", kind="tags", default_branch="trunk"),
            ),
            "acme/base": (
                CachedRef(name="trunk", kind="heads", default_branch="main"),
                CachedRef(name="v2.0.0", kind="tags", default_branch="main"),
            ),
        },
    )

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/site",
            ref=None,
            source_key="source:prod",
            direction="deps",
            depth=1,
        )
    )
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["acme/site@trunk"].ref_kind == "heads"
    assert nodes["acme/site@trunk"].default_branch == "trunk"
    assert nodes["acme/site@v2.0.0"].ref_kind == "tags"
    assert nodes["acme/site@v2.0.0"].default_branch == "trunk"
    assert nodes["acme/base@trunk"].ref_kind == "heads"
    assert nodes["acme/base@trunk"].default_branch == "main"
    assert nodes["acme/base@v2.0.0"].ref_kind == "tags"
    assert nodes["acme/base@v2.0.0"].default_branch == "main"


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
    # Converging paths must request the shared node from the index only once,
    # and only through batch reads -- never through point reads.
    batched_pairs = [pair for call in index.dependencies_batch_calls for pair in call]
    assert batched_pairs.count(("acme/shared", "main")) == 1
    assert index.dependency_calls == {}


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
    # Converging paths must request the shared node from the index only once,
    # and only through batch reads -- never through point reads.
    batched_pairs = [pair for call in index.dependents_batch_calls for pair in call]
    assert batched_pairs.count(("acme/shared", "main")) == 1
    assert index.dependent_calls == {}


def test_depth_fanout_issues_one_dependencies_batch_read_per_level() -> None:
    edges: list[IndexedDependency] = []
    cached_refs: dict[str, set[str]] = {"acme/root": {"main"}}
    for child_n in range(4):
        child = f"acme/child-{child_n}"
        cached_refs[child] = {"main"}
        edges.append(
            IndexedDependency(
                source_repo="acme/root",
                source_ref="main",
                dependency_repo=child,
                dependency_name=f"child-{child_n}",
                dependency_version="main",
                source_path="roles/requirements.yml",
            )
        )
        for leaf_n in range(4):
            leaf = f"acme/leaf-{child_n}-{leaf_n}"
            cached_refs[leaf] = {"main"}
            edges.append(
                IndexedDependency(
                    source_repo=child,
                    source_ref="main",
                    dependency_repo=leaf,
                    dependency_name=f"leaf-{child_n}-{leaf_n}",
                    dependency_version="main",
                    source_path="roles/requirements.yml",
                )
            )
    index = StubIndex(edges, cached_refs=cached_refs)

    graph = BuildGraph(index)(
        GraphRequest(
            repo="acme/root",
            ref="main",
            source_key="source:prod",
            direction="deps",
            depth=3,
        )
    )

    assert len(graph.nodes) == 21
    assert len(graph.edges) == 20
    assert graph.warnings == ()
    # One bulk read per traversal level instead of one point read per node.
    assert [len(pairs) for pairs in index.dependencies_batch_calls] == [1, 4, 16]
    assert index.dependency_calls == {}
    assert index.dependent_calls == {}
