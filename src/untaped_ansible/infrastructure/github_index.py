"""GitHub-backed dependency read adapter for live dependency graphing."""

from __future__ import annotations

from untaped_ansible.application.ports import (
    DependencyIndex,
    GitHubDependencyReader,
    IndexedDependency,
)
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.parser import parse_dependency_file


class GithubDependencyIndex:
    """Read declared dependencies live from GitHub, with indexed impact fallback."""

    def __init__(
        self,
        *,
        github: GitHubDependencyReader,
        wrapped: DependencyIndex,
        aliases: dict[str, str],
        dependency_paths: list[str],
    ) -> None:
        self._github = github
        self._wrapped = wrapped
        self._aliases = aliases
        self._dependency_paths = dependency_paths
        self._cache: dict[tuple[str, str | None], list[IndexedDependency]] = {}

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        scope: str | None,
    ) -> list[IndexedDependency]:
        key = (repo, ref)
        if key not in self._cache:
            self._cache[key] = self._live_dependencies(repo, ref)
        return self._cache[key]

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        scope: str | None,
    ) -> list[IndexedDependency]:
        return self._wrapped.dependents(repo, ref, scope=scope)

    def is_stale(self, scope: str | None, *, max_age_seconds: int) -> bool:
        return self._wrapped.is_stale(scope, max_age_seconds=max_age_seconds)

    def _live_dependencies(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        owner, name = _split_repo(repo)
        read_ref = ref or _default_branch(self._github.get_repository(owner, name))
        paths = self._tree_paths(owner, name, read_ref)
        resolver = IdentityResolver(self._aliases)
        edges: list[IndexedDependency] = []
        for path in self._dependency_paths:
            if path not in paths:
                continue
            report = parse_dependency_file(
                path,
                self._github.get_raw_content(owner, name, path, ref=read_ref),
            )
            for declaration in report.dependencies:
                resolved = resolver.resolve(declaration)
                edges.append(
                    IndexedDependency(
                        source_repo=repo,
                        source_ref=ref,
                        dependency_repo=resolved.repo,
                        dependency_name=declaration.name,
                        dependency_version=declaration.version,
                        source_path=path,
                        unresolved=resolved.unresolved,
                    )
                )
        return edges

    def _tree_paths(self, owner: str, repo: str, ref: str) -> set[str]:
        tree = self._github.get_tree(owner, repo, ref, recursive=True).get("tree")
        if not isinstance(tree, list):
            return set()
        paths = set()
        for entry in tree:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str) and path:
                paths.add(path)
        return paths


def _split_repo(repo: str) -> tuple[str, str]:
    owner, separator, name = repo.partition("/")
    if not owner or not separator or not name:
        raise ValueError(f"repo must be owner/name (got {repo!r})")
    return owner, name


def _default_branch(repository: dict[str, object]) -> str:
    default_branch = repository.get("default_branch")
    if isinstance(default_branch, str) and default_branch:
        return default_branch
    return "HEAD"
