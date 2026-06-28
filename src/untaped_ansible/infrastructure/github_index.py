"""GitHub-backed dependency read adapter for live dependency graphing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.domain.payloads import CachedRef, IndexedDependency, SkippedDependencyFile

if TYPE_CHECKING:
    from collections.abc import Sequence

    from untaped_ansible.application.ports import DependencyIndex, GitHubDependencyReader


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
        self._warnings: list[SkippedDependencyFile] = []

    @property
    def warnings(self) -> tuple[SkippedDependencyFile, ...]:
        """Parse warnings accumulated during live dependency reads."""
        return tuple(self._warnings)

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
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
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return self._wrapped.dependents(repo, ref, source_key=source_key)

    def dependencies_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        # Intentionally per-pair: live reads (per-repo tree/content fetches) don't batch.
        return {
            (repo, ref): self.dependencies(repo, ref, source_key=source_key)
            for repo, ref in dict.fromkeys(pairs)
        }

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        return self._wrapped.dependents_batch(pairs, source_key=source_key)

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        refs = set(self._wrapped.cached_refs(repo, source_key=source_key))
        refs.update(
            cached_ref
            for cached_repo, cached_ref in self._cache
            if cached_repo == repo and cached_ref is not None
        )
        return refs

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        return self._with_live_refs(
            repo,
            self._wrapped.cached_ref_metadata(repo, source_key=source_key),
        )

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[CachedRef, ...]]:
        wrapped = self._wrapped.cached_ref_metadata_batch(repos, source_key=source_key)
        return {repo: self._with_live_refs(repo, metadata) for repo, metadata in wrapped.items()}

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self._wrapped.is_stale(source_key, max_age_seconds=max_age_seconds)

    def _with_live_refs(
        self,
        repo: str,
        wrapped: tuple[CachedRef, ...],
    ) -> tuple[CachedRef, ...]:
        metadata = list(wrapped)
        known = {(cached_ref.name, cached_ref.kind) for cached_ref in metadata}
        for cached_repo, cached_ref in self._cache:
            if cached_repo != repo or cached_ref is None or (cached_ref, None) in known:
                continue
            metadata.append(CachedRef(name=cached_ref))
        return tuple(metadata)

    def _live_dependencies(self, repo: str, ref: str | None) -> list[IndexedDependency]:
        owner, name = _split_repo(repo)
        read_ref = self._read_ref(owner, name, ref)
        source_ref = ref or read_ref
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
            self._warnings.extend(
                SkippedDependencyFile(
                    repo=repo,
                    ref=source_ref,
                    source_path=warning.source_path,
                    reason=warning.reason,
                )
                for warning in report.warnings
            )
            for declaration in report.dependencies:
                resolved = resolver.resolve(declaration)
                edges.append(
                    IndexedDependency(
                        source_repo=repo,
                        source_ref=source_ref,
                        dependency_repo=resolved.repo,
                        dependency_name=declaration.name,
                        dependency_version=declaration.version,
                        source_path=path,
                        unresolved=resolved.unresolved,
                    )
                )
        return edges

    def _read_ref(self, owner: str, repo: str, ref: str | None) -> str:
        if ref is None:
            return _default_branch(self._github.get_repository(owner, repo))
        return self._resolved_ref_sha(owner, repo, ref) or ref

    def _resolved_ref_sha(self, owner: str, repo: str, ref: str) -> str | None:
        for namespace in ("heads", "tags"):
            expected_ref = f"refs/{namespace}/{ref}"
            for row in self._github.list_matching_refs(owner, repo, f"{namespace}/{ref}"):
                if not isinstance(row, dict) or row.get("ref") != expected_ref:
                    continue
                ref_object = row.get("object")
                if not isinstance(ref_object, dict):
                    continue
                sha = ref_object.get("sha")
                if isinstance(sha, str) and sha:
                    return sha
        return None

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
