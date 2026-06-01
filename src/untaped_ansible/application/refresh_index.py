"""Use case for refreshing a dependency source index from GitHub."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import Any

from pydantic import BaseModel, ConfigDict

from untaped_ansible.application.ports import (
    DependencyIndexWriter,
    GitHubDependencyReader,
    IndexedDependency,
    IndexScan,
)
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.settings import SourceDefinition, normalize_team_refs


class RefreshResult(BaseModel):
    """Summary of an index refresh."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    repos: int
    refs: int
    edges: int
    ignored_collections: tuple[str, ...] = ()


class RefreshSourceIndex:
    """Scan GitHub dependency files and replace one source in the index."""

    def __init__(
        self,
        *,
        github: GitHubDependencyReader,
        index: DependencyIndexWriter,
        aliases: dict[str, str],
        default_dependency_paths: list[str],
    ) -> None:
        self._github = github
        self._index = index
        self._aliases = aliases
        self._default_dependency_paths = default_dependency_paths

    def __call__(self, source: SourceDefinition, *, source_key: str) -> RefreshResult:
        repos = self._expand_repos(source)
        dependencies: list[IndexedDependency] = []
        ignored_collections: set[str] = set()
        ref_count = 0
        for repo in repos:
            owner, name = repo.split("/", maxsplit=1)
            for ref_name, sha in self._refs(owner, name, source):
                ref_count += 1
                paths = source.dependency_paths or self._default_dependency_paths
                tree_paths = self._tree_paths(owner, name, sha)
                for path in paths:
                    if path not in tree_paths:
                        continue
                    report = parse_dependency_file(
                        path,
                        self._github.get_raw_content(owner, name, path, ref=sha),
                    )
                    ignored_collections.update(report.ignored_collections)
                    resolver = IdentityResolver(self._aliases)
                    for declaration in report.dependencies:
                        resolved = resolver.resolve(declaration)
                        dependencies.append(
                            IndexedDependency(
                                source_repo=repo,
                                source_ref=ref_name,
                                source_sha=sha,
                                dependency_repo=resolved.repo,
                                dependency_name=declaration.name,
                                dependency_version=declaration.version,
                                source_path=path,
                                unresolved=resolved.unresolved,
                            )
                        )
        scan = IndexScan(
            source_key=source_key,
            scanned_at=datetime.now(UTC),
            repos=len(repos),
            refs=ref_count,
            dependencies=tuple(dependencies),
        )
        self._index.replace_source_scan(scan)
        return RefreshResult(
            source_key=source_key,
            repos=len(repos),
            refs=ref_count,
            edges=len(dependencies),
            ignored_collections=tuple(sorted(ignored_collections)),
        )

    def _expand_repos(self, source: SourceDefinition) -> list[str]:
        repos = list(source.repos)
        for org in source.orgs:
            repos.extend(_repo_names(self._github.list_org_repos(org)))
        for team in normalize_team_refs(source.teams, source.orgs):
            org, slug = _split_team(team)
            repos.extend(_repo_names(self._github.list_team_repos(org, slug)))
        return sorted(dict.fromkeys(repos))

    def _refs(self, owner: str, repo: str, source: SourceDefinition) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for kind in source.ref_kinds:
            patterns = list(source.ref_patterns)
            namespaces = _matching_ref_namespaces(kind, patterns)
            if not patterns and kind == "heads":
                default_branch = _default_branch(self._github.get_repository(owner, repo))
                patterns = [default_branch]
                namespaces = [f"{kind}/{default_branch}"]
            for namespace in namespaces:
                refs.extend(
                    self._filtered_refs(
                        owner,
                        repo,
                        kind=kind,
                        namespace=namespace,
                        patterns=patterns,
                    )
                )
        return sorted(dict.fromkeys(refs))

    def _filtered_refs(
        self,
        owner: str,
        repo: str,
        *,
        kind: str,
        namespace: str,
        patterns: list[str],
    ) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for row in self._github.list_matching_refs(owner, repo, namespace):
            full_ref = _str(row.get("ref"))
            sha = _object_sha(row.get("object"))
            prefix = f"refs/{kind}/"
            if full_ref is None or sha is None or not full_ref.startswith(prefix):
                continue
            name = full_ref.removeprefix(prefix)
            if patterns and not any(fnmatch(name, pattern) for pattern in patterns):
                continue
            refs.append((name, sha))
        return refs

    def _tree_paths(self, owner: str, repo: str, sha: str) -> set[str]:
        tree = self._github.get_tree(owner, repo, sha, recursive=True).get("tree")
        if not isinstance(tree, list):
            return set()
        paths = set()
        for entry in tree:
            if not isinstance(entry, dict):
                continue
            path = _str(entry.get("path"))
            if path is not None:
                paths.add(path)
        return paths


def _matching_ref_namespaces(kind: str, patterns: list[str]) -> list[str]:
    if not patterns:
        return [kind]
    namespaces: list[str] = []
    for pattern in patterns:
        literal_prefix = _safe_literal_ref_prefix(pattern)
        namespace = kind if literal_prefix == "" else f"{kind}/{literal_prefix}"
        if namespace == kind:
            return [kind]
        if any(namespace.startswith(existing) for existing in namespaces):
            continue
        namespaces = [existing for existing in namespaces if not existing.startswith(namespace)]
        namespaces.append(namespace)
    return namespaces


def _safe_literal_ref_prefix(pattern: str) -> str:
    if not _has_wildcard(pattern):
        return pattern
    prefix = _literal_ref_prefix(pattern)
    if "/" not in prefix:
        return ""
    return f"{prefix.rsplit('/', maxsplit=1)[0]}/"


def _has_wildcard(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


def _literal_ref_prefix(pattern: str) -> str:
    prefix = []
    for char in pattern:
        if char in "*?[":
            break
        prefix.append(char)
    return "".join(prefix)


def _default_branch(repository: dict[str, object]) -> str:
    default_branch = repository.get("default_branch")
    if isinstance(default_branch, str) and default_branch:
        return default_branch
    return "HEAD"


def _repo_names(rows: Iterable[dict[str, object]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = _str(row.get("full_name"))
        if name is not None:
            names.append(name)
    return names


def _split_team(value: str) -> tuple[str, str]:
    org, _, slug = value.partition("/")
    if not org or not slug:
        raise ValueError(f"team must be ORG/SLUG (got {value!r})")
    return org, slug


def _object_sha(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return _str(value.get("sha"))


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
