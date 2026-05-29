"""Use case for refreshing a named dependency index scope from GitHub."""

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
from untaped_ansible.settings import ScopeDefinition, normalize_team_refs


class RefreshResult(BaseModel):
    """Summary of an index refresh."""

    model_config = ConfigDict(frozen=True)

    scope: str
    repos: int
    refs: int
    edges: int
    ignored_collections: tuple[str, ...] = ()


class RefreshIndex:
    """Scan GitHub dependency files and replace one scope in the index."""

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

    def __call__(self, scope: ScopeDefinition) -> RefreshResult:
        repos = self._expand_repos(scope)
        dependencies: list[IndexedDependency] = []
        ignored_collections: set[str] = set()
        ref_count = 0
        for repo in repos:
            owner, name = repo.split("/", maxsplit=1)
            for ref_name, sha in self._refs(owner, name, scope):
                ref_count += 1
                paths = scope.dependency_paths or self._default_dependency_paths
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
            scope=scope.name,
            scanned_at=datetime.now(UTC),
            dependencies=tuple(dependencies),
        )
        self._index.replace_scope_scan(scan)
        return RefreshResult(
            scope=scope.name,
            repos=len(repos),
            refs=ref_count,
            edges=len(dependencies),
            ignored_collections=tuple(sorted(ignored_collections)),
        )

    def _expand_repos(self, scope: ScopeDefinition) -> list[str]:
        repos = list(scope.repos)
        for org in scope.orgs:
            repos.extend(_repo_names(self._github.list_org_repos(org)))
        for team in normalize_team_refs(scope.teams, scope.orgs):
            org, slug = _split_team(team)
            repos.extend(_repo_names(self._github.list_team_repos(org, slug)))
        return sorted(dict.fromkeys(repos))

    def _refs(self, owner: str, repo: str, scope: ScopeDefinition) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for kind in scope.ref_kinds:
            for row in self._github.list_matching_refs(owner, repo, kind):
                full_ref = _str(row.get("ref"))
                sha = _object_sha(row.get("object"))
                prefix = f"refs/{kind}/"
                if full_ref is None or sha is None or not full_ref.startswith(prefix):
                    continue
                name = full_ref.removeprefix(prefix)
                if scope.ref_patterns and not any(
                    fnmatch(name, pattern) for pattern in scope.ref_patterns
                ):
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
