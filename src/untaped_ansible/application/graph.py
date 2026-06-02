"""Use case for building dependency and reverse-impact graphs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from untaped_ansible.application.ports import DependencyIndex, IndexedDependency
from untaped_ansible.domain.graph import DependencyGraph, GraphEdge, GraphNode

GraphDirection = Literal["deps", "impact", "both"]


class GraphRequest(BaseModel):
    """Parameters for a graph query."""

    model_config = ConfigDict(frozen=True)

    repo: str
    ref: str | None = None
    source_key: str | None = None
    direction: GraphDirection = "both"
    depth: int | None = 3
    stale_after: int = 86_400


class BuildGraph:
    """Build a dependency graph from a cached dependency read port."""

    def __init__(self, index: DependencyIndex) -> None:
        self._index = index

    def __call__(self, request: GraphRequest) -> DependencyGraph:
        builder = _GraphBuilder(self._index, request)
        return builder.build()


class _GraphBuilder:
    def __init__(self, index: DependencyIndex, request: GraphRequest) -> None:
        self._index = index
        self._request = request
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        self._seen_edges: set[tuple[str, str, str]] = set()
        self._warnings: list[str] = []
        self._seen_warnings: set[str] = set()

    def build(self) -> DependencyGraph:
        target_id = _node_id(self._request.repo, self._request.ref)
        self._add_node(self._request.repo, self._request.ref)
        depth = self._request.depth
        if self._request.direction in {"deps", "both"}:
            self._walk_deps(
                self._request.repo,
                self._request.ref,
                remaining=depth,
                stack={target_id},
            )
        if self._request.direction in {"impact", "both"}:
            self._walk_impact(
                self._request.repo,
                self._request.ref,
                remaining=depth,
                stack={target_id},
            )
        warnings: list[str] = []
        if self._request.direction in {"impact", "both"} and self._index.is_stale(
            self._request.source_key,
            max_age_seconds=self._request.stale_after,
        ):
            warnings.append("source data is stale; refresh it before relying on upstream impact")
        warnings.extend(self._warnings)
        return DependencyGraph(
            target_id=target_id,
            nodes=tuple(self._nodes.values()),
            edges=tuple(self._edges),
            warnings=tuple(warnings),
        )

    def _walk_deps(
        self,
        repo: str,
        ref: str | None,
        *,
        remaining: int | None,
        stack: set[str],
    ) -> None:
        if remaining == 0:
            return
        next_remaining = None if remaining is None else remaining - 1
        dependencies = self._index.dependencies(repo, ref, source_key=self._request.source_key)
        if not dependencies:
            self._warn_if_missing_cached_ref(repo, ref)
            return
        for indexed in dependencies:
            source_ref = ref if ref is not None else indexed.source_ref
            source_id = self._add_node(repo, source_ref)
            source_stack = {*stack, source_id}
            target_id = self._target_node_for_dependency(indexed)
            if target_id in source_stack:
                continue
            self._add_edge(source_id, target_id, "requires", indexed)
            if indexed.dependency_repo is None:
                continue
            self._walk_deps(
                indexed.dependency_repo,
                indexed.dependency_version,
                remaining=next_remaining,
                stack={*source_stack, target_id},
            )

    def _walk_impact(
        self,
        repo: str,
        ref: str | None,
        *,
        remaining: int | None,
        stack: set[str],
    ) -> None:
        if remaining == 0:
            return
        next_remaining = None if remaining is None else remaining - 1
        for indexed in self._index.dependents(repo, ref, source_key=self._request.source_key):
            target_ref = ref if ref is not None else indexed.dependency_version
            target_id = self._add_node(repo, target_ref)
            target_stack = {*stack, target_id}
            source_id = self._add_node(indexed.source_repo, indexed.source_ref)
            if source_id in target_stack:
                continue
            self._add_edge(source_id, target_id, "impacts", indexed)
            self._walk_impact(
                indexed.source_repo,
                indexed.source_ref,
                remaining=next_remaining,
                stack={*target_stack, source_id},
            )

    def _target_node_for_dependency(self, indexed: IndexedDependency) -> str:
        if indexed.dependency_repo is not None:
            return self._add_node(indexed.dependency_repo, indexed.dependency_version)
        unresolved = indexed.unresolved or indexed.dependency_name
        node_id = f"unresolved:{unresolved}"
        self._nodes.setdefault(
            node_id,
            GraphNode(id=node_id, label=f"unresolved: {unresolved}", unresolved=unresolved),
        )
        self._add_warning(
            f"unresolved dependency {unresolved} from "
            f"{_node_id(indexed.source_repo, indexed.source_ref)} in {indexed.source_path}"
        )
        return node_id

    def _add_node(self, repo: str, ref: str | None) -> str:
        node_id = _node_id(repo, ref)
        self._nodes.setdefault(
            node_id,
            GraphNode(id=node_id, label=_label(repo, ref), repo=repo, ref=ref),
        )
        return node_id

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: Literal["requires", "impacts"],
        indexed: IndexedDependency,
    ) -> None:
        key = (source_id, target_id, relation)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self._edges.append(
            GraphEdge(
                source_id=source_id,
                target_id=target_id,
                relation=relation,
                source_path=indexed.source_path,
                version=indexed.dependency_version,
            )
        )

    def _add_warning(self, warning: str) -> None:
        if warning in self._seen_warnings:
            return
        self._seen_warnings.add(warning)
        self._warnings.append(warning)

    def _warn_if_missing_cached_ref(self, repo: str, ref: str | None) -> None:
        if self._request.source_key is None or ref is None:
            return
        cached_refs = self._index.cached_refs(repo, source_key=self._request.source_key)
        node = _node_id(repo, ref)
        if ref in cached_refs:
            return
        if cached_refs:
            available = ", ".join(sorted(cached_refs))
            self._add_warning(
                f"not expanding {node} from cached source data: ref is not cached "
                f"(available refs: {available}). Scan the matching ref/tag or use --live "
                "for downstream."
            )
            return
        self._add_warning(
            f"not expanding {node} from cached source data: repo/ref is not cached. "
            "Add it to the source, scan the matching ref/tag, or use --live for downstream."
        )


def _node_id(repo: str, ref: str | None) -> str:
    return f"{repo}@{ref}" if ref else repo


def _label(repo: str, ref: str | None) -> str:
    return _node_id(repo, ref)
