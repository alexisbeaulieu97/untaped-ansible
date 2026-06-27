"""GraphQL-primary ref probe with bounded Git fallback."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from untaped_github import GithubGraphqlError

from untaped_ansible.domain.payloads import (
    GRAPHQL_RATE_LIMIT_FALLBACK,
    GRAPHQL_TRANSIENT_FALLBACK,
    ProbeReport,
    ProbeTarget,
)

BackendMode = Literal["auto", "graphql", "git"]
_FALLBACK_KINDS = {"transient", "chunk"}


class _RefProbe(Protocol):
    def probe(
        self,
        repos: Sequence[ProbeTarget],
        *,
        kinds: Sequence[str],
        mode: Literal["all", "default_branch"] = "all",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ProbeReport: ...


class AutoRefProbe:
    """Run GraphQL first, then Git for recoverable GraphQL probe failures."""

    def __init__(
        self,
        graphql: _RefProbe,
        git: _RefProbe,
        *,
        backend: BackendMode,
    ) -> None:
        if backend not in {"auto", "graphql", "git"}:
            raise ValueError("backend must be auto, graphql, or git")
        self._graphql = graphql
        self._git = git
        self._backend = backend

    def probe(
        self,
        repos: Sequence[ProbeTarget],
        *,
        kinds: Sequence[str],
        mode: Literal["all", "default_branch"] = "all",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ProbeReport:
        if self._backend == "graphql":
            return self._graphql.probe(repos, kinds=kinds, mode=mode, on_progress=on_progress)
        if self._backend == "git":
            return self._git.probe(repos, kinds=kinds, mode=mode, on_progress=on_progress)
        auto_progress = _monotonic_progress(len(repos), on_progress)
        try:
            report = self._graphql.probe(repos, kinds=kinds, mode=mode, on_progress=auto_progress)
        except GithubGraphqlError as exc:
            if exc.kind != "rate_limited":
                raise
            git_report = self._git.probe(repos, kinds=kinds, mode=mode, on_progress=auto_progress)
            return _with_fallbacks(
                git_report,
                repos=[target.full_name for target in repos],
                reason=GRAPHQL_RATE_LIMIT_FALLBACK,
            )
        fallback_targets = [
            target
            for target in repos
            if (failure := report.failures.get(target.full_name)) is not None
            and failure.kind in _FALLBACK_KINDS
        ]
        if not fallback_targets:
            return report
        git_report = self._git.probe(
            fallback_targets,
            kinds=kinds,
            mode=mode,
            on_progress=None,
        )
        return _merge_fallback(
            report,
            git_report,
            fallback_repos=[target.full_name for target in fallback_targets],
            reason=GRAPHQL_TRANSIENT_FALLBACK,
        )


def _with_fallbacks(report: ProbeReport, *, repos: list[str], reason: str) -> ProbeReport:
    fallbacks = dict(report.fallbacks)
    fallbacks.update({repo: reason for repo in repos})
    return report.model_copy(update={"fallbacks": fallbacks})


def _merge_fallback(
    graphql_report: ProbeReport,
    git_report: ProbeReport,
    *,
    fallback_repos: list[str],
    reason: str,
) -> ProbeReport:
    fallback_set = set(fallback_repos)
    repos = {
        repo: probed for repo, probed in graphql_report.repos.items() if repo not in fallback_set
    }
    repos.update(git_report.repos)
    failures = {
        repo: failure
        for repo, failure in graphql_report.failures.items()
        if repo not in fallback_set
    }
    failures.update(git_report.failures)
    fallbacks = dict(graphql_report.fallbacks)
    fallbacks.update({repo: reason for repo in fallback_repos})
    return graphql_report.model_copy(
        update={
            "repos": repos,
            "failures": failures,
            "fallbacks": fallbacks,
        }
    )


def _monotonic_progress(
    total: int,
    on_progress: Callable[[int, int], None] | None,
) -> Callable[[int, int], None] | None:
    if on_progress is None:
        return None

    max_done = 0

    def report(done: int, _total: int) -> None:
        nonlocal max_done
        clamped = max(0, min(done, total))
        if clamped <= max_done:
            return
        max_done = clamped
        on_progress(max_done, total)

    return report
