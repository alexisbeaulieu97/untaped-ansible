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
    scope: str | None = None
    direction: GraphDirection = "both"
    depth: int | None = 3
    stale_after: int = 86_400


class BuildGraph:
    """Build a dependency graph from an indexed dependency read port."""

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
        if self._index.is_stale(self._request.scope, max_age_seconds=self._request.stale_after):
            scope = self._request.scope or "default"
            warnings.append(f"scope index is stale: {scope}")
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
        source_id = _node_id(repo, ref)
        for indexed in self._index.dependencies(repo, ref, scope=self._request.scope):
            target_id = self._target_node_for_dependency(indexed)
            if target_id in stack:
                continue
            self._add_edge(source_id, target_id, "requires", indexed)
            if indexed.dependency_repo is None:
                continue
            self._walk_deps(
                indexed.dependency_repo,
                indexed.dependency_version,
                remaining=next_remaining,
                stack={*stack, target_id},
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
        target_id = _node_id(repo, ref)
        for indexed in self._index.dependents(repo, ref, scope=self._request.scope):
            source_id = self._add_node(indexed.source_repo, indexed.source_ref)
            if source_id in stack:
                continue
            self._add_edge(source_id, target_id, "impacts", indexed)
            self._walk_impact(
                indexed.source_repo,
                indexed.source_ref,
                remaining=next_remaining,
                stack={*stack, source_id},
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


def _node_id(repo: str, ref: str | None) -> str:
    return f"{repo}@{ref}" if ref else repo


def _label(repo: str, ref: str | None) -> str:
    return _node_id(repo, ref)
