"""Use case for refreshing a dependency source index from a bare Git cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from threading import Lock
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from untaped_ansible.application.ports import (
    GitHubDependencyReader,
    GitRef,
    IncrementalDependencyIndexWriter,
    IndexedDependency,
    RefScan,
    RefScanTouch,
)
from untaped_ansible.application.refresh_index import RefreshResult, _matching_ref_namespaces
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.settings import SourceDefinition, normalize_team_refs


class GitCache(Protocol):
    """Git operations needed by git-backed source refresh."""

    def ensure_bare(
        self,
        url: str,
        *,
        cache_dir: Path,
        auth_header: str | None,
    ) -> Path: ...

    def fetch_refs(
        self,
        bare_path: Path,
        *,
        refspecs: list[str],
        depth: int,
        blob_filter: bool,
        auth_header: str | None,
    ) -> None: ...

    def list_refs(self, bare_path: Path, kind: str) -> list[GitRef]: ...

    def read_file(
        self,
        bare_path: Path,
        sha: str,
        path: str,
        *,
        auth_header: str | None,
    ) -> str | None: ...


class _RepoCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str
    default_branch: str
    clone_url: str | None = None
    ssh_url: str | None = None
    html_url: str | None = None


class _RefSelection(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    patterns: tuple[str, ...]
    refspecs: tuple[str, ...]


@dataclass(frozen=True)
class _RepoRefreshResult:
    selected: frozenset[tuple[str, str, str]]
    scans: tuple[RefScan, ...]
    touches: tuple[RefScanTouch, ...]
    ignored_collections: frozenset[str]


class RefreshGitSourceIndex:
    """Refresh dependency source data through Git transport and local objects."""

    def __init__(
        self,
        *,
        github: GitHubDependencyReader,
        git: GitCache,
        index: IncrementalDependencyIndexWriter,
        aliases: dict[str, str],
        default_dependency_paths: list[str],
        repo_cache_path: Path,
        clone_protocol: str,
        fetch_depth: int,
        blob_filter: bool,
        auth_header: str | None,
        concurrency: int = 8,
    ) -> None:
        if clone_protocol not in {"https", "ssh"}:
            raise ValueError("clone_protocol must be 'https' or 'ssh'")
        if concurrency < 1 or concurrency > 32:
            raise ValueError("concurrency must be between 1 and 32")
        self._github = github
        self._git = git
        self._index = index
        self._aliases = aliases
        self._default_dependency_paths = default_dependency_paths
        self._repo_cache_path = repo_cache_path
        self._clone_protocol = clone_protocol
        self._fetch_depth = fetch_depth
        self._blob_filter = blob_filter
        self._auth_header = auth_header if clone_protocol == "https" else None
        self._concurrency = concurrency
        self._index_lock = Lock()

    def __call__(self, source: SourceDefinition, *, source_key: str) -> RefreshResult:
        repos = self._expand_repos(source)
        selected: set[tuple[str, str, str]] = set()
        ignored_collections: set[str] = set()
        paths = source.dependency_paths or self._default_dependency_paths
        paths_fingerprint = _dependency_paths_fingerprint(paths)
        aliases_fingerprint = _aliases_fingerprint(self._aliases)
        checked_at = datetime.now(UTC)
        pending_scans: list[RefScan] = []
        pending_touches: list[RefScanTouch] = []

        def refresh_one(repo: _RepoCandidate) -> _RepoRefreshResult:
            return self._refresh_repo(
                repo,
                source=source,
                source_key=source_key,
                paths=paths,
                paths_fingerprint=paths_fingerprint,
                aliases_fingerprint=aliases_fingerprint,
                checked_at=checked_at,
            )

        if len(repos) <= 1 or self._concurrency == 1:
            results = [refresh_one(repo) for repo in repos]
        else:
            with ThreadPoolExecutor(max_workers=min(self._concurrency, len(repos))) as executor:
                results = list(executor.map(refresh_one, repos))

        for result in results:
            selected.update(result.selected)
            ignored_collections.update(result.ignored_collections)
            pending_scans.extend(result.scans)
            pending_touches.extend(result.touches)

        self._index.commit_source_ref_refresh(
            source_key,
            scans=tuple(pending_scans),
            touches=tuple(pending_touches),
            keep=selected,
            scanned_at=checked_at,
        )
        status = self._index.status(source_key)
        return RefreshResult(
            source_key=source_key,
            repos=len(repos),
            refs=len(selected),
            edges=0 if status is None else status.edges,
            ignored_collections=tuple(sorted(ignored_collections)),
            changed_refs=len(pending_scans),
            unchanged_refs=len(pending_touches),
        )

    def _refresh_repo(
        self,
        repo: _RepoCandidate,
        *,
        source: SourceDefinition,
        source_key: str,
        paths: list[str],
        paths_fingerprint: str,
        aliases_fingerprint: str,
        checked_at: datetime,
    ) -> _RepoRefreshResult:
        selected: set[tuple[str, str, str]] = set()
        ignored_collections: set[str] = set()
        pending_scans: list[RefScan] = []
        pending_touches: list[RefScanTouch] = []
        clone_url = _clone_url(repo, self._clone_protocol)
        bare = self._git.ensure_bare(
            clone_url,
            cache_dir=self._repo_cache_path,
            auth_header=self._auth_header,
        )
        selections = _ref_selections(source, repo.default_branch)
        refspecs = sorted({refspec for selection in selections for refspec in selection.refspecs})
        self._git.fetch_refs(
            bare,
            refspecs=refspecs,
            depth=self._fetch_depth,
            blob_filter=self._blob_filter,
            auth_header=self._auth_header,
        )
        for ref in _selected_refs(self._git, bare, selections):
            selected.add((repo.full_name, ref.kind, ref.name))
            with self._index_lock:
                metadata = self._index.ref_scan(source_key, repo.full_name, ref.kind, ref.name)
            if (
                metadata is not None
                and metadata.source_sha == ref.sha
                and metadata.backend == "git"
                and metadata.dependency_paths_fingerprint == paths_fingerprint
                and metadata.aliases_fingerprint == aliases_fingerprint
                and metadata.clone_protocol == self._clone_protocol
                and metadata.clone_url == clone_url
            ):
                pending_touches.append(
                    RefScanTouch(
                        source_key=source_key,
                        source_repo=repo.full_name,
                        ref_kind=ref.kind,
                        source_ref=ref.name,
                        checked_at=checked_at,
                    )
                )
                continue
            edges, ignored = self._read_dependencies(
                bare,
                repo=repo.full_name,
                ref=ref,
                paths=paths,
            )
            ignored_collections.update(ignored)
            pending_scans.append(
                RefScan(
                    source_key=source_key,
                    source_repo=repo.full_name,
                    ref_kind=ref.kind,
                    source_ref=ref.name,
                    source_sha=ref.sha,
                    backend="git",
                    clone_url=clone_url,
                    clone_protocol=self._clone_protocol,
                    dependency_paths_fingerprint=paths_fingerprint,
                    aliases_fingerprint=aliases_fingerprint,
                    checked_at=checked_at,
                    indexed_at=checked_at,
                    dependencies=tuple(edges),
                )
            )

        return _RepoRefreshResult(
            selected=frozenset(selected),
            scans=tuple(pending_scans),
            touches=tuple(pending_touches),
            ignored_collections=frozenset(ignored_collections),
        )

    def _expand_repos(self, source: SourceDefinition) -> list[_RepoCandidate]:
        repos: dict[str, _RepoCandidate] = {}
        for repo in source.repos:
            owner, name = repo.split("/", maxsplit=1)
            candidate = _repo_candidate(self._github.get_repository(owner, name), fallback=repo)
            repos[candidate.full_name] = candidate
        for org in source.orgs:
            for candidate in _repo_candidates(self._github.list_org_repos(org)):
                repos.setdefault(candidate.full_name, candidate)
        for team in normalize_team_refs(source.teams, source.orgs):
            org, slug = _split_team(team)
            for candidate in _repo_candidates(self._github.list_team_repos(org, slug)):
                repos.setdefault(candidate.full_name, candidate)
        return [repos[name] for name in sorted(repos)]

    def _read_dependencies(
        self,
        bare: Path,
        *,
        repo: str,
        ref: GitRef,
        paths: list[str],
    ) -> tuple[list[IndexedDependency], set[str]]:
        edges: list[IndexedDependency] = []
        ignored_collections: set[str] = set()
        resolver = IdentityResolver(self._aliases)
        for path in paths:
            content = self._git.read_file(
                bare,
                ref.sha,
                path,
                auth_header=self._auth_header,
            )
            if content is None:
                continue
            report = parse_dependency_file(path, content)
            ignored_collections.update(report.ignored_collections)
            for declaration in report.dependencies:
                resolved = resolver.resolve(declaration)
                edges.append(
                    IndexedDependency(
                        source_repo=repo,
                        source_ref=ref.name,
                        source_sha=ref.sha,
                        dependency_repo=resolved.repo,
                        dependency_name=declaration.name,
                        dependency_version=declaration.version,
                        source_path=path,
                        unresolved=resolved.unresolved,
                    )
                )
        return edges, ignored_collections


def _ref_selections(source: SourceDefinition, default_branch: str) -> list[_RefSelection]:
    selections: list[_RefSelection] = []
    for kind in source.ref_kinds:
        patterns = tuple(source.ref_patterns or ([default_branch] if kind == "heads" else []))
        namespaces = _matching_ref_namespaces(kind, list(patterns))
        refspecs = tuple(_namespace_refspec(namespace) for namespace in namespaces)
        selections.append(_RefSelection(kind=kind, patterns=patterns, refspecs=refspecs))
    return selections


def _namespace_refspec(namespace: str) -> str:
    kind, _, suffix = namespace.partition("/")
    if not suffix:
        return f"+refs/{kind}/*:refs/{kind}/*"
    if suffix.endswith("/"):
        return f"+refs/{kind}/{suffix}*:refs/{kind}/{suffix}*"
    return f"+refs/{kind}/{suffix}:refs/{kind}/{suffix}"


def _selected_refs(git: GitCache, bare: Path, selections: list[_RefSelection]) -> list[GitRef]:
    selected: dict[tuple[str, str], GitRef] = {}
    for selection in selections:
        for ref in git.list_refs(bare, selection.kind):
            if selection.patterns and not any(
                fnmatch(ref.name, pattern) for pattern in selection.patterns
            ):
                continue
            selected[(ref.kind, ref.name)] = ref
    return [selected[key] for key in sorted(selected)]


def _repo_candidates(rows: Iterable[dict[str, object]]) -> list[_RepoCandidate]:
    return [_repo_candidate(row, fallback=None) for row in rows if _str(row.get("full_name"))]


def _repo_candidate(row: dict[str, object], *, fallback: str | None) -> _RepoCandidate:
    full_name = _str(row.get("full_name")) or fallback
    if full_name is None:
        raise ValueError("repository metadata missing full_name")
    default_branch = _str(row.get("default_branch")) or "main"
    return _RepoCandidate(
        full_name=full_name,
        default_branch=default_branch,
        clone_url=_str(row.get("clone_url")),
        ssh_url=_str(row.get("ssh_url")),
        html_url=_str(row.get("html_url")),
    )


def _clone_url(repo: _RepoCandidate, clone_protocol: str) -> str:
    if clone_protocol == "ssh":
        return repo.ssh_url or f"git@github.com:{repo.full_name}.git"
    if repo.clone_url is not None:
        return repo.clone_url
    if repo.html_url is not None:
        return f"{repo.html_url.rstrip('/')}.git"
    return f"https://github.com/{repo.full_name}.git"


def _dependency_paths_fingerprint(paths: list[str]) -> str:
    payload = json.dumps(paths, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _aliases_fingerprint(aliases: dict[str, str]) -> str:
    payload = json.dumps(aliases, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _split_team(value: str) -> tuple[str, str]:
    org, _, slug = value.partition("/")
    if not org or not slug:
        raise ValueError(f"team must be ORG/SLUG (got {value!r})")
    return org, slug


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
