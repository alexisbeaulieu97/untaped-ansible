"""Shared source-refresh wiring for the source and graph CLI commands."""

from __future__ import annotations

import time
from base64 import b64encode
from collections.abc import Callable

from untaped.api import HttpSettings, ProgressHandle, UiContext, echo
from untaped_github import GithubClient, GithubSettings

from untaped_ansible.application.refresh_git_index import RefreshGitSourceIndex
from untaped_ansible.application.refresh_index import RefreshResult
from untaped_ansible.domain.payloads import RefreshProgressEvent
from untaped_ansible.infrastructure import (
    GithubRefProbe,
    GitRepositoryCache,
    SqliteDependencyIndex,
)
from untaped_ansible.settings import AnsibleSettings, SourceDefinition

_LOW_RATE_LIMIT_THRESHOLD = 500


def run_source_refresh(
    source: SourceDefinition,
    *,
    source_key: str,
    action: str,
    label: str,
    index: SqliteDependencyIndex,
    aliases: dict[str, str],
    settings: AnsibleSettings,
    github_settings: GithubSettings,
    http: HttpSettings,
    concurrency: int,
    ui: UiContext,
) -> RefreshResult:
    """Refresh one source with stderr progress, then echo summary and warnings."""
    started_at = time.perf_counter()
    with ui.progress(f"Refreshing {label}") as progress:
        result = refresh_source(
            source,
            source_key=source_key,
            index=index,
            aliases=aliases,
            settings=settings,
            github_settings=github_settings,
            http=http,
            concurrency=concurrency,
            on_progress=progress_reporter(progress),
        )
    echo(
        refresh_summary(
            action,
            label,
            result,
            concurrency=concurrency,
            elapsed=time.perf_counter() - started_at,
        ),
        err=True,
    )
    warn_low_rate_limit(result)
    return result


def refresh_source(
    source: SourceDefinition,
    *,
    source_key: str,
    index: SqliteDependencyIndex,
    aliases: dict[str, str],
    settings: AnsibleSettings,
    github_settings: GithubSettings,
    http: HttpSettings,
    concurrency: int,
    on_progress: Callable[[RefreshProgressEvent], None] | None = None,
) -> RefreshResult:
    """Run a git-backed source refresh with fully wired adapters."""
    with GithubClient(github_settings, http=http) as github:
        token = (
            github_settings.token.get_secret_value().strip()
            if github_settings.token is not None
            else ""
        )
        result = RefreshGitSourceIndex(
            github=github,
            git=GitRepositoryCache(),
            probe=GithubRefProbe(github, concurrency=settings.probe_concurrency),
            index=index,
            aliases=aliases,
            default_dependency_paths=settings.dependency_paths,
            repo_cache_path=settings.repo_cache_path,
            clone_protocol=settings.git_clone_protocol,
            fetch_depth=settings.git_fetch_depth,
            blob_filter=settings.git_blob_filter,
            auth_header=_git_auth_header(token) if token else None,
            concurrency=concurrency,
            probe_concurrency=settings.probe_concurrency,
            ref_scan_default=settings.ref_scan_default,
            on_progress=on_progress,
        )(source, source_key=source_key)
    return result


def progress_reporter(handle: ProgressHandle) -> Callable[[RefreshProgressEvent], None]:
    """Adapt refresh progress events onto a core UiContext progress handle."""
    last_phase: str | None = None

    def report(event: RefreshProgressEvent) -> None:
        nonlocal last_phase
        new_phase = event.phase != last_phase
        last_phase = event.phase
        fraction = event.done / event.total if event.total else None
        handle.update(_format_progress(event), fraction=fraction, new_phase=new_phase)

    return report


def refresh_summary(
    action: str,
    label: str,
    result: RefreshResult,
    *,
    concurrency: int,
    elapsed: float,
) -> str:
    """One-line stderr summary for a completed source refresh."""
    message = (
        f"{action} {label}: {result.repos} repos, {result.refs} refs, {result.edges} edges, "
        f"{result.changed_refs} changed, {result.unchanged_refs} unchanged in {elapsed:.2f}s"
    )
    return f"{message} (concurrency {concurrency})"


def warn_low_rate_limit(result: RefreshResult) -> None:
    """Warn on stderr when the GraphQL rate limit budget is running out."""
    remaining = result.rate_limit_remaining
    if remaining is not None and remaining < _LOW_RATE_LIMIT_THRESHOLD:
        echo(
            f"warning: GitHub GraphQL rate limit is low: {remaining} points remaining",
            err=True,
        )


def pluralize(count: int, noun: str) -> str:
    """Render ``count`` with a naively pluralized noun, e.g. ``2 repo failures``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _format_progress(event: RefreshProgressEvent) -> str:
    if event.phase == "expanding":
        return f"expanding source: {event.done}/{event.total} selectors"
    noun = "probing refs" if event.phase == "probing" else "fetching changes"
    message = f"{noun}: {event.done}/{event.total} repos"
    if event.changed is not None:
        message = f"{message}, {event.changed} changed"
    return message


def _git_auth_header(token: str) -> str:
    credential = b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {credential}"
