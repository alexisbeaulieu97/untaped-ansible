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
from untaped_github import (
    RepositoryInventoryScope,
    ResolveRepositoryInventory,
    normalize_team_scopes,
)

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
    ProbeReport,
    RefreshProgressEvent,
    RefScan,
    RefScanMetadata,
    RefScanTouch,
    RepoFailure,
    SourceRepoMetadata,
)
from untaped_ansible.settings import SourceDefinition

ProgressCallback = Callable[[RefreshProgressEvent], None]
ProbeMode = Literal["all", "default_branch"]

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
        repo_batch_size: int = 100,
        rate_limit_floor: int = 500,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        if clone_protocol not in {"https", "ssh"}:
            raise ValueError("clone_protocol must be 'https' or 'ssh'")
        if concurrency < 1 or concurrency > 32:
            raise ValueError("concurrency must be between 1 and 32")
        if probe_concurrency < 1 or probe_concurrency > 32:
            raise ValueError("probe_concurrency must be between 1 and 32")
        if repo_batch_size < 1:
            raise ValueError("repo_batch_size must be >= 1")
        if rate_limit_floor < 0:
            raise ValueError("rate_limit_floor must be >= 0")
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
        self._repo_batch_size = repo_batch_size
        self._rate_limit_floor = rate_limit_floor
        self._on_progress = on_progress

    def __call__(self, source: SourceDefinition, *, source_key: str) -> RefreshResult:
        repos = self._expand_repos(source)
        paths = source.dependency_paths or self._default_dependency_paths
        paths_fingerprint = _dependency_paths_fingerprint(paths)
        aliases_fingerprint = _aliases_fingerprint(self._aliases)
        source_fingerprint = _source_refresh_fingerprint(
            source,
            repos=repos,
            paths_fingerprint=paths_fingerprint,
            aliases_fingerprint=aliases_fingerprint,
            ref_scan_default=self._effective_ref_scan_default(source),
            clone_protocol=self._clone_protocol,
            fetch_depth=self._fetch_depth,
            blob_filter=self._blob_filter,
        )
        progress = self._index.refresh_progress(source_key, source_fingerprint)
        failures: dict[str, str] = {
            repo: status.removeprefix("failure:")
            for repo, status in progress.items()
            if status.startswith("failure:")
        }
        successful_repos: set[str] = {
            repo for repo, status in progress.items() if status == "success"
        }
        pending_repos = [repo for repo in repos if repo.full_name not in progress]
        selected: set[tuple[str, str, str]] = set()
        ignored_collections: set[str] = set()
        checked_at = datetime.now(UTC)
        changed_refs = 0
        unchanged_refs = 0
        rate_limit_cost: int | None = None
        rate_limit_remaining: int | None = None
        rate_limit_reset_at: datetime | None = None
        probe_kinds = self._probe_kinds(source)
        probe_mode = self._probe_mode(source)

        for batch_index, batch in enumerate(_chunks(pending_repos, self._repo_batch_size)):
            probe_report = self._probe.probe(
                [repo.full_name for repo in batch],
                kinds=probe_kinds,
                mode=probe_mode,
                on_progress=self._probe_progress,
            )
            rate_limit_cost, rate_limit_remaining, rate_limit_reset_at = _merge_rate_limit(
                rate_limit_cost,
                rate_limit_remaining,
                rate_limit_reset_at,
                probe_report,
            )
            batch_failures: dict[str, str] = dict(probe_report.failures)
            tasks = [
                self._repo_refresh_task(source, repo, probe_report.repos[repo.full_name])
                for repo in batch
                if repo.full_name in probe_report.repos
            ]
            batch_selected: set[tuple[str, str, str]] = set()
            batch_scans: list[RefScan] = []
            batch_touches: list[RefScanTouch] = []
            batch_repo_metadata: list[SourceRepoMetadata] = []
            batch_statuses: dict[str, str] = {}

            for full_name, outcome in self._refresh_repos(
                tasks,
                source_key=source_key,
                paths=paths,
                checked_at=checked_at,
            ):
                if isinstance(outcome, str):
                    batch_failures[full_name] = outcome
                    continue
                successful_repos.add(full_name)
                batch_statuses[full_name] = "success"
                batch_selected.update(outcome.selected)
                ignored_collections.update(outcome.ignored_collections)
                batch_scans.extend(outcome.scans)
                batch_touches.extend(outcome.touches)
                batch_repo_metadata.append(outcome.repo_metadata)

            for repo, reason in batch_failures.items():
                failures[repo] = reason
                batch_statuses[repo] = f"failure:{reason}"

            if batch_statuses:
                selected.update(batch_selected)
                changed_refs += len(batch_scans)
                unchanged_refs += len(batch_touches)
                self._index.commit_source_ref_partial_refresh(
                    source_key,
                    scans=tuple(batch_scans),
                    touches=tuple(batch_touches),
                    keep=batch_selected,
                    repo_metadata=tuple(batch_repo_metadata),
                    processed_repos=frozenset(batch_statuses) - frozenset(batch_failures),
                )
                self._index.mark_refresh_progress(source_key, source_fingerprint, batch_statuses)

            repos_left = len(pending_repos) - (batch_index + 1) * self._repo_batch_size
            if (
                repos_left > 0
                and rate_limit_remaining is not None
                and rate_limit_remaining < self._rate_limit_floor
            ):
                return self._refresh_result(
                    source_key=source_key,
                    repos=repos,
                    selected=selected,
                    ignored_collections=ignored_collections,
                    changed_refs=changed_refs,
                    unchanged_refs=unchanged_refs,
                    failures=failures,
                    rate_limit_cost=rate_limit_cost,
                    rate_limit_remaining=rate_limit_remaining,
                    rate_limit_reset_at=rate_limit_reset_at,
                    completed=False,
                    pause_reason=_rate_limit_pause_reason(rate_limit_remaining),
                )

        # When every expanded repo failed there is nothing trustworthy to mark
        # complete: leave cached data and freshness untouched so the run does
        # not look fresh. An empty source is a successful refresh and still
        # prunes now-unselected repos.
        if not repos or successful_repos:
            self._index.complete_source_ref_refresh(
                source_key,
                source_repos=frozenset(repo.full_name for repo in repos),
                scanned_at=checked_at,
            )
        else:
            self._index.clear_refresh_progress(source_key)
        return self._refresh_result(
            source_key=source_key,
            repos=repos,
            selected=selected,
            ignored_collections=ignored_collections,
            changed_refs=changed_refs,
            unchanged_refs=unchanged_refs,
            failures=failures,
            rate_limit_cost=rate_limit_cost,
            rate_limit_remaining=rate_limit_remaining,
            rate_limit_reset_at=rate_limit_reset_at,
            completed=True,
        )

    def _refresh_result(
        self,
        *,
        source_key: str,
        repos: list[_RepoCandidate],
        selected: set[tuple[str, str, str]],
        ignored_collections: set[str],
        changed_refs: int,
        unchanged_refs: int,
        failures: dict[str, str],
        rate_limit_cost: int | None,
        rate_limit_remaining: int | None,
        rate_limit_reset_at: datetime | None,
        completed: bool,
        pause_reason: str | None = None,
    ) -> RefreshResult:
        status = self._index.status(source_key)
        refs = status.refs if completed and status is not None else len(selected)
        edges = status.edges if completed and status is not None else 0
        return RefreshResult(
            source_key=source_key,
            completed=completed,
            pause_reason=pause_reason,
            repos=len(repos),
            refs=refs,
            edges=edges,
            ignored_collections=tuple(sorted(ignored_collections)),
            changed_refs=changed_refs,
            unchanged_refs=unchanged_refs,
            failures=tuple(
                RepoFailure(repo=repo, reason=failures[repo]) for repo in sorted(failures)
            ),
            rate_limit_cost=rate_limit_cost,
            rate_limit_remaining=rate_limit_remaining,
            rate_limit_reset_at=rate_limit_reset_at,
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
            ref_scan_default=self._effective_ref_scan_default(source),
        )
        return tuple(dict.fromkeys(selection.kind for selection in selections))

    def _effective_ref_scan_default(self, source: SourceDefinition) -> RefScanDefault:
        return source.ref_scan_default or self._ref_scan_default

    def _probe_mode(self, source: SourceDefinition) -> ProbeMode:
        if (
            self._effective_ref_scan_default(source) == "default_branch"
            and not source.ref_kinds
            and not source.ref_patterns
        ):
            return "default_branch"
        return "all"

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
            ref_scan_default=self._effective_ref_scan_default(source),
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
        """Expand explicit repos, orgs, and teams into candidates.

        Expansion failures propagate: an unknown org/team/repo is a source
        misconfiguration, not a per-repo refresh failure.
        """
        selector_count = len(source.repos) + len(source.orgs) + len(source.teams)
        inventory = ResolveRepositoryInventory(self._github)(
            RepositoryInventoryScope(
                orgs=tuple(source.orgs),
                teams=normalize_team_scopes(source.teams, orgs=tuple(source.orgs)),
                repos=tuple(source.repos),
            )
        )
        self._emit_progress("expanding", done=selector_count, total=selector_count)
        return [_repo_candidate(item.model_dump(), fallback=None) for item in inventory]

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


def _source_refresh_fingerprint(
    source: SourceDefinition,
    *,
    repos: list[_RepoCandidate],
    paths_fingerprint: str,
    aliases_fingerprint: str,
    ref_scan_default: RefScanDefault,
    clone_protocol: str,
    fetch_depth: int,
    blob_filter: bool,
) -> str:
    payload = {
        "source": source.model_dump(mode="json", exclude={"name"}),
        "repos": [repo.full_name for repo in repos],
        "paths_fingerprint": paths_fingerprint,
        "aliases_fingerprint": aliases_fingerprint,
        "ref_scan_default": ref_scan_default,
        "clone_protocol": clone_protocol,
        "fetch_depth": fetch_depth,
        "blob_filter": blob_filter,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _merge_rate_limit(
    current_cost: int | None,
    current_remaining: int | None,
    current_reset_at: datetime | None,
    report: ProbeReport,
) -> tuple[int | None, int | None, datetime | None]:
    cost = current_cost
    if report.rate_limit_cost is not None:
        cost = report.rate_limit_cost if cost is None else cost + report.rate_limit_cost
    remaining = current_remaining
    if report.rate_limit_remaining is not None:
        remaining = (
            report.rate_limit_remaining
            if remaining is None
            else min(remaining, report.rate_limit_remaining)
        )
    reset_at = report.rate_limit_reset_at or current_reset_at
    return cost, remaining, reset_at


def _rate_limit_pause_reason(remaining: int) -> str:
    return f"GitHub GraphQL rate limit is low: {remaining} points remaining"


def _chunks[T](values: list[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _split_team(value: str) -> tuple[str, str]:
    org, _, slug = value.partition("/")
    if not org or not slug:
        raise ValueError(f"team must be ORG/SLUG (got {value!r})")
    return org, slug


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
