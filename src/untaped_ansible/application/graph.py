"""Use case for building dependency and reverse-impact graphs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, NamedTuple, Protocol, assert_never

from pydantic import BaseModel, ConfigDict

from untaped_ansible.application.ports import DependencyIndex
from untaped_ansible.domain.cycles import detect_cycles
from untaped_ansible.domain.graph import DependencyGraph, GraphEdge, GraphNode
from untaped_ansible.domain.payloads import CachedRef, IndexedDependency
from untaped_ansible.domain.ref_display import RefDisplay, sort_ref_displays

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
    refresh_hint: str | None = None
    """Caller-composed fix instruction appended to stale/missing-ref warnings.

    The application layer stays free of CLI strings; the CLI passes the exact
    refresh command (or flag guidance) to surface in actionable warnings.
    """


class BuildGraph:
    """Build a dependency graph from a cached dependency read port."""

    def __init__(self, index: DependencyIndex) -> None:
        self._index = index

    def __call__(self, request: GraphRequest) -> DependencyGraph:
        builder = _GraphBuilder(self._index, request)
        return builder.build()


class _AddNodeItem(NamedTuple):
    repo: str
    ref: str | None
    ref_kind: str | None


class _AddTargetItem(NamedTuple):
    indexed: IndexedDependency


class _AddEdgeItem(NamedTuple):
    source_id: str
    target_id: str
    relation: Literal["requires", "impacts"]
    indexed: IndexedDependency


class _MissingRefItem(NamedTuple):
    repo: str
    ref: str | None


_ReplayItem = _AddNodeItem | _AddTargetItem | _AddEdgeItem | _MissingRefItem


class _EdgeBatchRead(Protocol):
    """Batch edge read matching ``dependencies_batch``/``dependents_batch``."""

    def __call__(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]: ...


class _Walk:
    """One frontier entry: a node to expand carrying its own traversal path stack."""

    __slots__ = ("items", "ref", "remaining", "repo", "stack")

    def __init__(
        self,
        repo: str,
        ref: str | None,
        remaining: int | None,
        stack: set[str],
    ) -> None:
        self.repo = repo
        self.ref = ref
        self.remaining = remaining
        self.stack = stack
        self.items: list[_ReplayItem | _Walk] = []


class _GraphBuilder:
    def __init__(self, index: DependencyIndex, request: GraphRequest) -> None:
        self._index = index
        self._request = request
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        self._seen_edges: set[tuple[str, str, str]] = set()
        self._warnings: list[str] = []
        self._seen_warnings: set[str] = set()
        self._dependencies: dict[tuple[str, str | None, str | None], list[IndexedDependency]] = {}
        self._dependents: dict[tuple[str, str | None, str | None], list[IndexedDependency]] = {}
        self._cached_refs: dict[tuple[str, str | None], set[str]] = {}
        self._cached_ref_metadata: dict[tuple[str, str | None], tuple[CachedRef, ...]] = {}

    def build(self) -> DependencyGraph:
        target_id = _node_id(self._request.repo, self._request.ref)
        self._add_node(self._request.repo, self._request.ref)
        depth = self._request.depth
        if self._request.direction in {"deps", "both"}:
            self._walk(
                _Walk(self._request.repo, self._request.ref, depth, {target_id}),
                expand=self._expand_deps,
                prefetch=self._prefetch_deps_level,
            )
        if self._request.direction in {"impact", "both"}:
            self._walk(
                _Walk(self._request.repo, self._request.ref, depth, {target_id}),
                expand=self._expand_impact,
                prefetch=self._prefetch_impact_level,
            )
        warnings: list[str] = []
        if self._request.direction in {"impact", "both"} and self._index.is_stale(
            self._request.source_key,
            max_age_seconds=self._request.stale_after,
        ):
            warnings.append(
                self._with_refresh_hint(
                    "source data is stale; refresh it before relying on upstream impact"
                )
            )
        warnings.extend(self._warnings)
        cycles, cycle_warnings = detect_cycles(self._edges)
        warnings.extend(cycle_warnings)
        return DependencyGraph(
            target_id=target_id,
            nodes=tuple(self._nodes.values()),
            edges=tuple(self._edges),
            cycles=cycles,
            warnings=tuple(warnings),
        )

    def _walk(
        self,
        root: _Walk,
        *,
        expand: Callable[[_Walk], list[_Walk]],
        prefetch: Callable[[list[_Walk]], None],
    ) -> None:
        """Expand the graph level by level, then replay emissions in DFS order.

        Each depth level's uncached index reads are bulk-loaded before any
        entry in that level is expanded, so expansion reads only from the
        per-run caches. Emissions are recorded per entry and replayed
        depth-first afterwards, keeping node/edge/warning ordering identical
        to the previous recursive depth-first traversal.
        """
        level = [root]
        while level:
            prefetch(level)
            level = [child for entry in level for child in expand(entry)]
        self._replay(root)

    def _expand_deps(self, entry: _Walk) -> list[_Walk]:
        if entry.remaining == 0:
            return []
        next_remaining = None if entry.remaining is None else entry.remaining - 1
        dependencies = self._dependencies_for(entry.repo, entry.ref)
        if not dependencies:
            entry.items.append(_MissingRefItem(entry.repo, entry.ref))
            return []
        children: list[_Walk] = []
        for indexed in dependencies:
            source_ref = entry.ref if entry.ref is not None else indexed.source_ref
            source_id = _node_id(entry.repo, source_ref)
            entry.items.append(_AddNodeItem(entry.repo, source_ref, indexed.source_ref_kind))
            source_stack = {*entry.stack, source_id}
            target_id = _dependency_target_id(indexed)
            entry.items.append(_AddTargetItem(indexed))
            entry.items.append(_AddEdgeItem(source_id, target_id, "requires", indexed))
            if target_id in source_stack:
                continue
            if indexed.dependency_repo is None:
                continue
            child = _Walk(
                indexed.dependency_repo,
                indexed.dependency_version,
                next_remaining,
                {*source_stack, target_id},
            )
            entry.items.append(child)
            children.append(child)
        return children

    def _expand_impact(self, entry: _Walk) -> list[_Walk]:
        if entry.remaining == 0:
            return []
        next_remaining = None if entry.remaining is None else entry.remaining - 1
        children: list[_Walk] = []
        for indexed in self._dependents_for(entry.repo, entry.ref):
            target_ref = entry.ref if entry.ref is not None else indexed.dependency_version
            target_id = _node_id(entry.repo, target_ref)
            entry.items.append(_AddNodeItem(entry.repo, target_ref, None))
            target_stack = {*entry.stack, target_id}
            source_id = _node_id(indexed.source_repo, indexed.source_ref)
            entry.items.append(
                _AddNodeItem(indexed.source_repo, indexed.source_ref, indexed.source_ref_kind)
            )
            entry.items.append(_AddEdgeItem(source_id, target_id, "impacts", indexed))
            if source_id in target_stack:
                continue
            child = _Walk(
                indexed.source_repo,
                indexed.source_ref,
                next_remaining,
                {*target_stack, source_id},
            )
            entry.items.append(child)
            children.append(child)
        return children

    def _replay(self, entry: _Walk) -> None:
        for item in entry.items:
            if isinstance(item, _Walk):
                self._replay(item)
            elif isinstance(item, _AddNodeItem):
                self._add_node(item.repo, item.ref, ref_kind=item.ref_kind)
            elif isinstance(item, _AddTargetItem):
                self._emit_target_node(item.indexed)
            elif isinstance(item, _AddEdgeItem):
                self._add_edge(item.source_id, item.target_id, item.relation, item.indexed)
            elif isinstance(item, _MissingRefItem):
                # Intentionally unbatched: missing-ref warnings are a rare path,
                # so the prefetch sets are not extended to cover its reads.
                self._warn_if_missing_cached_ref(item.repo, item.ref)
            else:
                assert_never(item)

    def _prefetch_deps_level(self, level: list[_Walk]) -> None:
        # Must mirror the read conditions of _expand_deps + _node_metadata.
        self._prefetch_edges(level, cache=self._dependencies, batch=self._index.dependencies_batch)
        repos: set[str] = set()
        for entry in level:
            if entry.remaining == 0:
                continue
            for indexed in self._dependencies_for(entry.repo, entry.ref):
                source_ref = entry.ref if entry.ref is not None else indexed.source_ref
                if source_ref is not None:
                    repos.add(entry.repo)
                if indexed.dependency_repo is not None and indexed.dependency_version is not None:
                    repos.add(indexed.dependency_repo)
        self._prefetch_ref_metadata(repos)

    def _prefetch_impact_level(self, level: list[_Walk]) -> None:
        # Must mirror the read conditions of _expand_impact + _node_metadata.
        self._prefetch_edges(level, cache=self._dependents, batch=self._index.dependents_batch)
        repos: set[str] = set()
        for entry in level:
            if entry.remaining == 0:
                continue
            for indexed in self._dependents_for(entry.repo, entry.ref):
                target_ref = entry.ref if entry.ref is not None else indexed.dependency_version
                if target_ref is not None:
                    repos.add(entry.repo)
                if indexed.source_ref is not None:
                    repos.add(indexed.source_repo)
        self._prefetch_ref_metadata(repos)

    def _prefetch_edges(
        self,
        level: list[_Walk],
        *,
        cache: dict[tuple[str, str | None, str | None], list[IndexedDependency]],
        batch: _EdgeBatchRead,
    ) -> None:
        source_key = self._request.source_key
        pairs = list(
            dict.fromkeys(
                (entry.repo, entry.ref)
                for entry in level
                if entry.remaining != 0 and (entry.repo, entry.ref, source_key) not in cache
            )
        )
        if not pairs:
            return
        loaded = batch(pairs, source_key=source_key)
        for repo, ref in pairs:
            cache[(repo, ref, source_key)] = loaded[(repo, ref)]

    def _prefetch_ref_metadata(self, repos: set[str]) -> None:
        source_key = self._request.source_key
        missing = sorted(
            repo for repo in repos if (repo, source_key) not in self._cached_ref_metadata
        )
        if not missing:
            return
        loaded = self._index.cached_ref_metadata_batch(missing, source_key=source_key)
        for repo in missing:
            self._cached_ref_metadata[(repo, source_key)] = loaded[repo]

    def _emit_target_node(self, indexed: IndexedDependency) -> None:
        """Emit the node (and any warning) for a dependency's target.

        The target node id itself comes from :func:`_dependency_target_id`.
        """
        if indexed.dependency_repo is not None:
            self._add_node(indexed.dependency_repo, indexed.dependency_version)
            return
        node_id = _dependency_target_id(indexed)
        unresolved = indexed.unresolved or indexed.dependency_name
        self._nodes.setdefault(
            node_id,
            GraphNode(id=node_id, label=f"unresolved: {unresolved}", unresolved=unresolved),
        )
        self._add_warning(
            f"unresolved dependency {unresolved} from "
            f"{_node_id(indexed.source_repo, indexed.source_ref)} in {indexed.source_path}"
        )

    def _add_node(self, repo: str, ref: str | None, *, ref_kind: str | None = None) -> str:
        node_id = _node_id(repo, ref)
        metadata = self._node_metadata(repo, ref, explicit_ref_kind=ref_kind)
        node = self._nodes.setdefault(
            node_id,
            GraphNode(
                id=node_id,
                label=_label(repo, ref),
                repo=repo,
                ref=ref,
                ref_kind=metadata.ref_kind,
                default_branch=metadata.default_branch,
            ),
        )
        updates: dict[str, str] = {}
        if node.ref_kind is None and metadata.ref_kind is not None:
            updates["ref_kind"] = metadata.ref_kind
        if node.default_branch is None and metadata.default_branch is not None:
            updates["default_branch"] = metadata.default_branch
        if updates:
            self._nodes[node_id] = node.model_copy(update=updates)
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
        cached_refs = self._cached_refs_for(repo)
        node = _node_id(repo, ref)
        if ref in cached_refs:
            return
        if cached_refs:
            available = ", ".join(self._sorted_cached_ref_names(repo, cached_refs))
            self._add_warning(
                self._with_refresh_hint(
                    f"not expanding {node} from cached source data: ref is not cached "
                    f"(available refs: {available}). Scan the matching ref/tag or use --live "
                    "for downstream."
                )
            )
            return
        self._add_warning(
            self._with_refresh_hint(
                f"not expanding {node} from cached source data: repo/ref is not cached. "
                "Add it to the source, scan the matching ref/tag, or use --live for downstream."
            )
        )

    def _with_refresh_hint(self, message: str) -> str:
        hint = self._request.refresh_hint
        if hint is None:
            return message
        separator = " " if message.endswith(".") else ". "
        return f"{message}{separator}{hint}"

    def _dependencies_for(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        key = (repo, ref, self._request.source_key)
        if key not in self._dependencies:
            self._dependencies[key] = self._index.dependencies(
                repo,
                ref,
                source_key=self._request.source_key,
            )
        return self._dependencies[key]

    def _dependents_for(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        key = (repo, ref, self._request.source_key)
        if key not in self._dependents:
            self._dependents[key] = self._index.dependents(
                repo,
                ref,
                source_key=self._request.source_key,
            )
        return self._dependents[key]

    def _cached_refs_for(self, repo: str) -> set[str]:
        key = (repo, self._request.source_key)
        if key not in self._cached_refs:
            self._cached_refs[key] = self._index.cached_refs(
                repo,
                source_key=self._request.source_key,
            )
        return self._cached_refs[key]

    def _cached_ref_metadata_for(self, repo: str) -> tuple[CachedRef, ...]:
        key = (repo, self._request.source_key)
        if key not in self._cached_ref_metadata:
            self._cached_ref_metadata[key] = self._index.cached_ref_metadata(
                repo,
                source_key=self._request.source_key,
            )
        return self._cached_ref_metadata[key]

    def _node_metadata(
        self,
        repo: str,
        ref: str | None,
        *,
        explicit_ref_kind: str | None,
    ) -> _NodeMetadata:
        if ref is None:
            return _NodeMetadata(ref_kind=explicit_ref_kind, default_branch=None)
        metadata = self._cached_ref_metadata_for(repo)
        matches = [cached_ref for cached_ref in metadata if cached_ref.name == ref]
        default_branch = _first_default_branch(matches) or _first_default_branch(metadata)
        if explicit_ref_kind is not None:
            return _NodeMetadata(ref_kind=explicit_ref_kind, default_branch=default_branch)
        kinds = {cached_ref.kind for cached_ref in matches if cached_ref.kind is not None}
        ref_kind = next(iter(kinds)) if len(kinds) == 1 else None
        return _NodeMetadata(ref_kind=ref_kind, default_branch=default_branch)

    def _sorted_cached_ref_names(self, repo: str, cached_refs: set[str]) -> list[str]:
        metadata = list(self._cached_ref_metadata_for(repo))
        names_with_metadata = {cached_ref.name for cached_ref in metadata}
        metadata.extend(CachedRef(name=name) for name in sorted(cached_refs - names_with_metadata))
        sorted_refs = sort_ref_displays(
            RefDisplay(
                name=cached_ref.name,
                kind=cached_ref.kind,
                default_branch=cached_ref.default_branch,
            )
            for cached_ref in metadata
        )
        names: list[str] = []
        seen: set[str] = set()
        for ref in sorted_refs:
            if ref.name in seen:
                continue
            seen.add(ref.name)
            names.append(ref.name)
        return names


class _NodeMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref_kind: str | None
    default_branch: str | None


def _node_id(repo: str, ref: str | None) -> str:
    return f"{repo}@{ref}" if ref else repo


def _dependency_target_id(indexed: IndexedDependency) -> str:
    """Compute a dependency's target node id without emitting the node."""
    if indexed.dependency_repo is not None:
        return _node_id(indexed.dependency_repo, indexed.dependency_version)
    return f"unresolved:{indexed.unresolved or indexed.dependency_name}"


def _label(repo: str, ref: str | None) -> str:
    return _node_id(repo, ref)


def _first_default_branch(refs: list[CachedRef] | tuple[CachedRef, ...]) -> str | None:
    for ref in refs:
        if ref.default_branch is not None:
            return ref.default_branch
    return None
