"""Use case for refreshing a dependency source index from a bare Git cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict
from untaped.api import UntapedError

from untaped_ansible._concurrency import bounded_map
from untaped_ansible.application.ports import (
    GitHubDependencyReader,
    IncrementalDependencyIndexWriter,
    RefProbe,
)
from untaped_ansible.application.refresh_index import RefreshResult
from untaped_ansible.application.source_refs import (
    RefScanDefault,
    pattern_matches,
    source_ref_selections,
)
from untaped_ansible.domain.errors import GitCacheError
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.models import ParseReport
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.domain.payloads import (
    GitRef,
    IndexedDependency,
    ProbedRepo,
    RefreshProgressEvent,
    RefScan,
    RefScanMetadata,
    RefScanTouch,
    RepoFailure,
    SourceRepoMetadata,
)
from untaped_ansible.settings import SourceDefinition, normalize_team_refs

ProgressCallback = Callable[[RefreshProgressEvent], None]

# Errors a fetch/parse worker may raise: GitCacheError from the local Git
# cache and UntapedError from the SQLite index. The worker has no HTTP path
# (GitHub REST/GraphQL traffic happens in expansion and the probe), so no
# HTTP-specific error type belongs here.
_REPO_FAILURE_ERRORS = (GitCacheError, UntapedError)


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


@dataclass(frozen=True)
class _RepoRefreshResult:
    selected: frozenset[tuple[str, str, str]]
    scans: tuple[RefScan, ...]
    touches: tuple[RefScanTouch, ...]
    repo_metadata: SourceRepoMetadata
    ignored_collections: frozenset[str]


@dataclass(frozen=True)
class _RepoRefreshTask:
    repo: _RepoCandidate
    default_branch: str
    refs: tuple[GitRef, ...]


@dataclass(frozen=True)
class _ParsedDependencyFiles:
    reports: tuple[tuple[str, ParseReport], ...]
    ignored_collections: frozenset[str]


class RefreshGitSourceIndex:
    """Refresh dependency source data through Git transport and local objects."""

    def __init__(
        self,
        *,
        github: GitHubDependencyReader,
        git: GitCache,
        probe: RefProbe,
        index: IncrementalDependencyIndexWriter,
        aliases: dict[str, str],
        default_dependency_paths: list[str],
        repo_cache_path: Path,
        clone_protocol: str,
        fetch_depth: int,
        blob_filter: bool,
        auth_header: str | None,
        ref_scan_default: RefScanDefault = "all",
        concurrency: int = 8,
        probe_concurrency: int = 8,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        if clone_protocol not in {"https", "ssh"}:
            raise ValueError("clone_protocol must be 'https' or 'ssh'")
        if concurrency < 1 or concurrency > 32:
            raise ValueError("concurrency must be between 1 and 32")
        if probe_concurrency < 1 or probe_concurrency > 32:
            raise ValueError("probe_concurrency must be between 1 and 32")
        self._github = github
        self._git = git
        self._probe = probe
        self._index = index
        self._aliases = aliases
        self._default_dependency_paths = default_dependency_paths
        self._repo_cache_path = repo_cache_path
        self._clone_protocol = clone_protocol
        self._fetch_depth = fetch_depth
        self._blob_filter = blob_filter
        self._auth_header = auth_header if clone_protocol == "https" else None
        self._ref_scan_default = ref_scan_default
        self._concurrency = concurrency
        self._probe_concurrency = probe_concurrency
        self._on_progress = on_progress

    def __call__(self, source: SourceDefinition, *, source_key: str) -> RefreshResult:
        repos = self._expand_repos(source)
        probe_report = self._probe.probe(
            [repo.full_name for repo in repos],
            kinds=self._probe_kinds(source),
            on_progress=self._probe_progress,
        )
        failures: dict[str, str] = dict(probe_report.failures)
        tasks = [
            self._repo_refresh_task(source, repo, probe_report.repos[repo.full_name])
            for repo in repos
            if repo.full_name in probe_report.repos
        ]

        selected: set[tuple[str, str, str]] = set()
        ignored_collections: set[str] = set()
        paths = source.dependency_paths or self._default_dependency_paths
        checked_at = datetime.now(UTC)
        pending_scans: list[RefScan] = []
        pending_touches: list[RefScanTouch] = []
        pending_repo_metadata: list[SourceRepoMetadata] = []

        for full_name, outcome in self._refresh_repos(
            tasks,
            source_key=source_key,
            paths=paths,
            checked_at=checked_at,
        ):
            if isinstance(outcome, str):
                failures[full_name] = outcome
                continue
            selected.update(outcome.selected)
            ignored_collections.update(outcome.ignored_collections)
            pending_scans.extend(outcome.scans)
            pending_touches.extend(outcome.touches)
            pending_repo_metadata.append(outcome.repo_metadata)

        # When every repo failed there is nothing trustworthy to commit:
        # leave cached data and freshness (scanned_at) untouched so the run
        # does not look fresh. An empty source (zero repos expanded) is a
        # successful refresh and still commits.
        if not repos or len(failures) < len(repos):
            self._index.commit_source_ref_refresh(
                source_key,
                scans=tuple(pending_scans),
                touches=tuple(pending_touches),
                keep=selected,
                repo_metadata=tuple(pending_repo_metadata),
                scanned_at=checked_at,
                failed_repos=frozenset(failures),
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
            failures=tuple(
                RepoFailure(repo=repo, reason=failures[repo]) for repo in sorted(failures)
            ),
            rate_limit_remaining=probe_report.rate_limit_remaining,
        )

    def _refresh_repos(
        self,
        tasks: list[_RepoRefreshTask],
        *,
        source_key: str,
        paths: list[str],
        checked_at: datetime,
    ) -> Iterable[tuple[str, _RepoRefreshResult | str]]:
        """Run per-repo fetch/parse workers; collect results or failure reasons."""
        paths_fingerprint = _dependency_paths_fingerprint(paths)
        aliases_fingerprint = _aliases_fingerprint(self._aliases)
        total = len(tasks)
        done = 0
        changed = 0
        outcomes: list[tuple[str, _RepoRefreshResult | str]] = []

        def outcome_of(task: _RepoRefreshTask) -> _RepoRefreshResult | str:
            try:
                return self._refresh_repo(
                    task,
                    source_key=source_key,
                    paths=paths,
                    paths_fingerprint=paths_fingerprint,
                    aliases_fingerprint=aliases_fingerprint,
                    checked_at=checked_at,
                )
            except _REPO_FAILURE_ERRORS as exc:
                return str(exc) or type(exc).__name__

        def record(task: _RepoRefreshTask, outcome: _RepoRefreshResult | str) -> None:
            nonlocal done, changed
            done += 1
            changed += len(outcome.scans) if isinstance(outcome, _RepoRefreshResult) else 0
            self._emit_progress("fetching", done=done, total=total, changed=changed)
            outcomes.append((task.repo.full_name, outcome))

        bounded_map(outcome_of, tasks, concurrency=self._concurrency, on_each=record)
        return outcomes

    def _probe_kinds(self, source: SourceDefinition) -> tuple[str, ...]:
        """Union of ref kinds needed across all repos.

        Kind selection in :func:`source_ref_selections` depends only on
        ``ref_kinds``/``ref_patterns``/``ref_scan_default``; the default
        branch only narrows patterns, so any placeholder works here.
        """
        selections = source_ref_selections(
            source,
            default_branch="HEAD",
            ref_scan_default=self._ref_scan_default,
        )
        return tuple(dict.fromkeys(selection.kind for selection in selections))

    def _repo_refresh_task(
        self,
        source: SourceDefinition,
        repo: _RepoCandidate,
        probed: ProbedRepo,
    ) -> _RepoRefreshTask:
        default_branch = probed.default_branch or repo.default_branch
        selections = source_ref_selections(
            source,
            default_branch=default_branch,
            ref_scan_default=self._ref_scan_default,
        )
        selected: dict[tuple[str, str], GitRef] = {}
        for selection in selections:
            for ref in probed.refs:
                if ref.kind != selection.kind:
                    continue
                if not pattern_matches(ref.name, selection.patterns):
                    continue
                selected[(ref.kind, ref.name)] = ref
        return _RepoRefreshTask(
            repo=repo,
            default_branch=default_branch,
            refs=tuple(selected[key] for key in sorted(selected)),
        )

    def _refresh_repo(
        self,
        task: _RepoRefreshTask,
        *,
        source_key: str,
        paths: list[str],
        paths_fingerprint: str,
        aliases_fingerprint: str,
        checked_at: datetime,
    ) -> _RepoRefreshResult:
        repo = task.repo
        selected: set[tuple[str, str, str]] = set()
        ignored_collections: set[str] = set()
        pending_scans: list[RefScan] = []
        pending_touches: list[RefScanTouch] = []
        clone_url = _clone_url(repo, self._clone_protocol)
        metadata_by_ref = self._ref_scan_metadata(source_key, repo.full_name, task.refs)
        changed_refs: list[GitRef] = []
        for ref in task.refs:
            selected.add((repo.full_name, ref.kind, ref.name))
            metadata = metadata_by_ref.get((ref.kind, ref.name))
            if (
                metadata is not None
                and metadata.source_sha == ref.sha
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
            changed_refs.append(ref)
        repo_metadata = SourceRepoMetadata(
            source_key=source_key,
            source_repo=repo.full_name,
            default_branch=task.default_branch,
        )
        if not changed_refs:
            return _RepoRefreshResult(
                selected=frozenset(selected),
                scans=(),
                touches=tuple(pending_touches),
                repo_metadata=repo_metadata,
                ignored_collections=frozenset(),
            )

        bare = self._git.ensure_bare(
            clone_url,
            cache_dir=self._repo_cache_path,
            auth_header=self._auth_header,
        )
        self._git.fetch_refs(
            bare,
            refspecs=[_exact_refspec(ref) for ref in changed_refs],
            depth=self._fetch_depth,
            blob_filter=self._blob_filter,
            auth_header=self._auth_header,
        )
        parsed_by_sha: dict[str, _ParsedDependencyFiles] = {}
        resolver = IdentityResolver(self._aliases)
        for ref in changed_refs:
            parsed = parsed_by_sha.get(ref.sha)
            if parsed is None:
                parsed = self._read_dependency_files(
                    bare,
                    ref=ref,
                    paths=paths,
                )
                parsed_by_sha[ref.sha] = parsed
            ignored_collections.update(parsed.ignored_collections)
            edges = self._edges_from_reports(
                parsed,
                repo=repo.full_name,
                ref=ref,
                resolver=resolver,
            )
            pending_scans.append(
                RefScan(
                    source_key=source_key,
                    source_repo=repo.full_name,
                    ref_kind=ref.kind,
                    source_ref=ref.name,
                    source_sha=ref.sha,
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
            repo_metadata=repo_metadata,
            ignored_collections=frozenset(ignored_collections),
        )

    def _expand_repos(self, source: SourceDefinition) -> list[_RepoCandidate]:
        """Expand explicit repos, orgs, and teams into candidates, in parallel.

        Expansion failures propagate: an unknown org/team/repo is a source
        misconfiguration, not a per-repo refresh failure.
        """
        expansions: list[Callable[[], list[_RepoCandidate]]] = []

        def expand_repo(repo: str) -> Callable[[], list[_RepoCandidate]]:
            owner, name = repo.split("/", maxsplit=1)
            return lambda: [
                _repo_candidate(self._github.get_repository(owner, name), fallback=repo)
            ]

        def expand_org(org: str) -> Callable[[], list[_RepoCandidate]]:
            return lambda: _repo_candidates(self._github.list_org_repos(org))

        def expand_team(team: str) -> Callable[[], list[_RepoCandidate]]:
            org, slug = _split_team(team)
            return lambda: _repo_candidates(self._github.list_team_repos(org, slug))

        explicit_count = len(source.repos)
        expansions.extend(expand_repo(repo) for repo in source.repos)
        expansions.extend(expand_org(org) for org in source.orgs)
        expansions.extend(
            expand_team(team) for team in normalize_team_refs(source.teams, source.orgs)
        )

        results = self._run_expansions(expansions)

        repos: dict[str, _RepoCandidate] = {}
        for index, candidates in enumerate(results):
            for candidate in candidates:
                if index < explicit_count:
                    repos[candidate.full_name] = candidate
                else:
                    repos.setdefault(candidate.full_name, candidate)
        return [repos[name] for name in sorted(repos)]

    def _run_expansions(
        self,
        expansions: list[Callable[[], list[_RepoCandidate]]],
    ) -> list[list[_RepoCandidate]]:
        total = len(expansions)
        results: dict[int, list[_RepoCandidate]] = {}

        def expand(index: int) -> list[_RepoCandidate]:
            return expansions[index]()

        def record(index: int, candidates: list[_RepoCandidate]) -> None:
            results[index] = candidates
            self._emit_progress("expanding", done=len(results), total=total)

        bounded_map(expand, range(total), concurrency=self._probe_concurrency, on_each=record)
        return [results[index] for index in range(total)]

    def _probe_progress(self, done: int, total: int) -> None:
        self._emit_progress("probing", done=done, total=total)

    def _emit_progress(
        self,
        phase: Literal["expanding", "probing", "fetching"],
        *,
        done: int,
        total: int,
        changed: int | None = None,
    ) -> None:
        if self._on_progress is None:
            return
        self._on_progress(
            RefreshProgressEvent(phase=phase, done=done, total=total, changed=changed)
        )

    def _ref_scan_metadata(
        self,
        source_key: str,
        repo: str,
        refs: tuple[GitRef, ...],
    ) -> dict[tuple[str, str], RefScanMetadata]:
        return self._index.ref_scans(
            source_key,
            repo,
            ((ref.kind, ref.name) for ref in refs),
        )

    def _read_dependency_files(
        self,
        bare: Path,
        *,
        ref: GitRef,
        paths: list[str],
    ) -> _ParsedDependencyFiles:
        reports: list[tuple[str, ParseReport]] = []
        ignored_collections: set[str] = set()
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
            reports.append((path, report))
        return _ParsedDependencyFiles(
            reports=tuple(reports),
            ignored_collections=frozenset(ignored_collections),
        )

    def _edges_from_reports(
        self,
        parsed: _ParsedDependencyFiles,
        *,
        repo: str,
        ref: GitRef,
        resolver: IdentityResolver,
    ) -> list[IndexedDependency]:
        edges: list[IndexedDependency] = []
        for path, report in parsed.reports:
            for declaration in report.dependencies:
                resolved = resolver.resolve(declaration)
                edges.append(
                    IndexedDependency(
                        source_repo=repo,
                        source_ref=ref.name,
                        source_ref_kind=ref.kind,
                        source_sha=ref.sha,
                        dependency_repo=resolved.repo,
                        dependency_name=declaration.name,
                        dependency_version=declaration.version,
                        source_path=path,
                        unresolved=resolved.unresolved,
                    )
                )
        return edges


def _exact_refspec(ref: GitRef) -> str:
    full_ref = f"refs/{ref.kind}/{ref.name}"
    return f"+{full_ref}:{full_ref}"


def _repo_candidates(rows: Iterable[dict[str, object]]) -> list[_RepoCandidate]:
    return [_repo_candidate(row, fallback=None) for row in rows if _str(row.get("full_name"))]


def _repo_candidate(row: dict[str, object], *, fallback: str | None) -> _RepoCandidate:
    full_name = _str(row.get("full_name")) or fallback
    if full_name is None:
        raise ValueError("repository metadata missing full_name")
    default_branch = _str(row.get("default_branch")) or "HEAD"
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
