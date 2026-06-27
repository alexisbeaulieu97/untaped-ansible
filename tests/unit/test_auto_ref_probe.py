"""Tests for GraphQL-primary ref probe fallback orchestration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

import pytest
from untaped_github import GithubGraphqlError

from untaped_ansible.domain.payloads import (
    GRAPHQL_RATE_LIMIT_FALLBACK,
    GRAPHQL_TRANSIENT_FALLBACK,
    GitRef,
    ProbedRepo,
    ProbeFailure,
    ProbeReport,
    ProbeTarget,
)
from untaped_ansible.infrastructure.auto_ref_probe import AutoRefProbe


class FakeProbe:
    def __init__(self) -> None:
        self.report = ProbeReport()
        self.error: Exception | None = None
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []
        self.progress_events: list[tuple[int, int]] = []

    def probe(
        self,
        repos: Sequence[ProbeTarget],
        *,
        kinds: Sequence[str],
        mode: Literal["all", "default_branch"] = "all",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ProbeReport:
        self.calls.append((tuple(repo.full_name for repo in repos), tuple(kinds), mode))
        if on_progress is not None:
            events = self.progress_events or [(len(repos), len(repos))]
            for done, total in events:
                on_progress(done, total)
        if self.error is not None:
            raise self.error
        return self.report


def test_auto_graphql_success_never_calls_git() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.report = ProbeReport(repos={"acme/a": _probed("main", "sha-a")})

    report = AutoRefProbe(graphql, git, backend="auto").probe(_targets("acme/a"), kinds=("heads",))

    assert report.repos == graphql.report.repos
    assert git.calls == []


def test_auto_falls_back_transient_and_chunk_failures_only() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.report = ProbeReport(
        repos={"acme/ok": _probed("main", "sha-ok")},
        failures={
            "acme/transient": ProbeFailure(kind="transient", reason="transient"),
            "acme/chunk": ProbeFailure(kind="chunk", reason="chunk"),
            "acme/missing": ProbeFailure(kind="missing", reason="missing"),
        },
    )
    git.report = ProbeReport(
        repos={
            "acme/transient": _probed("main", "sha-transient"),
            "acme/chunk": _probed("main", "sha-chunk"),
        }
    )

    report = AutoRefProbe(graphql, git, backend="auto").probe(
        _targets("acme/ok", "acme/transient", "acme/chunk", "acme/missing"),
        kinds=("heads",),
    )

    assert set(report.repos) == {"acme/ok", "acme/transient", "acme/chunk"}
    assert report.failures == {"acme/missing": ProbeFailure(kind="missing", reason="missing")}
    assert git.calls == [(("acme/transient", "acme/chunk"), ("heads",), "all")]
    assert report.fallbacks == {
        "acme/transient": GRAPHQL_TRANSIENT_FALLBACK,
        "acme/chunk": GRAPHQL_TRANSIENT_FALLBACK,
    }


def test_auto_git_fallback_failures_replace_graphql_transient_failures() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.report = ProbeReport(
        failures={"acme/flaky": ProbeFailure(kind="transient", reason="transient")}
    )
    git.report = ProbeReport(
        failures={"acme/flaky": ProbeFailure(kind="git", reason="git ref probe failed: denied")}
    )

    report = AutoRefProbe(graphql, git, backend="auto").probe(
        _targets("acme/flaky"),
        kinds=("heads",),
    )

    assert report.repos == {}
    assert report.failures == {
        "acme/flaky": ProbeFailure(kind="git", reason="git ref probe failed: denied")
    }
    assert report.fallbacks == {"acme/flaky": GRAPHQL_TRANSIENT_FALLBACK}


def test_auto_primary_rate_limit_falls_back_entire_target_set() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.error = GithubGraphqlError(
        "github graphql rate limit exceeded",
        kind="rate_limited",
        status_code=403,
        url="https://api.github.com/graphql",
    )
    git.report = ProbeReport(
        repos={
            "acme/a": _probed("main", "sha-a"),
            "acme/b": _probed("main", "sha-b"),
        }
    )

    report = AutoRefProbe(graphql, git, backend="auto").probe(
        _targets("acme/a", "acme/b"),
        kinds=("heads",),
        mode="default_branch",
    )

    assert set(report.repos) == {"acme/a", "acme/b"}
    assert git.calls == [(("acme/a", "acme/b"), ("heads",), "default_branch")]
    assert report.fallbacks == {
        "acme/a": GRAPHQL_RATE_LIMIT_FALLBACK,
        "acme/b": GRAPHQL_RATE_LIMIT_FALLBACK,
    }


def test_auto_rate_limit_fallback_progress_is_monotonic_with_original_total() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.error = GithubGraphqlError("github graphql rate limit exceeded", kind="rate_limited")
    graphql.progress_events = [(1, 2)]
    git.progress_events = [(1, 2), (2, 2)]
    git.report = ProbeReport(
        repos={
            "acme/a": _probed("main", "sha-a"),
            "acme/b": _probed("main", "sha-b"),
        }
    )
    progress: list[tuple[int, int]] = []

    AutoRefProbe(graphql, git, backend="auto").probe(
        _targets("acme/a", "acme/b"),
        kinds=("heads",),
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert progress == [(1, 2), (2, 2)]
    assert all(total == 2 for _, total in progress)
    assert [done for done, _ in progress] == sorted(done for done, _ in progress)


def test_auto_transient_subset_fallback_does_not_emit_smaller_progress_total() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.progress_events = [(3, 3)]
    graphql.report = ProbeReport(
        repos={"acme/ok": _probed("main", "sha-ok")},
        failures={"acme/flaky": ProbeFailure(kind="transient", reason="transient")},
    )
    git.progress_events = [(1, 1)]
    git.report = ProbeReport(repos={"acme/flaky": _probed("main", "sha-flaky")})
    progress: list[tuple[int, int]] = []

    AutoRefProbe(graphql, git, backend="auto").probe(
        _targets("acme/ok", "acme/flaky", "acme/missing"),
        kinds=("heads",),
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert progress == [(3, 3)]


@pytest.mark.parametrize("kind", ["auth", "forbidden", "secondary_rate_limited", "unknown"])
def test_auto_does_not_fallback_other_global_graphql_errors(kind: str) -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.error = GithubGraphqlError("graphql failed", kind=kind)

    with pytest.raises(GithubGraphqlError):
        AutoRefProbe(graphql, git, backend="auto").probe(_targets("acme/a"), kinds=("heads",))

    assert git.calls == []


def test_explicit_graphql_and_git_modes_do_not_auto_fallback() -> None:
    graphql = FakeProbe()
    git = FakeProbe()
    graphql.report = ProbeReport(
        failures={"acme/flaky": ProbeFailure(kind="transient", reason="transient")}
    )
    git.report = ProbeReport(repos={"acme/flaky": _probed("main", "sha")})

    graphql_report = AutoRefProbe(graphql, git, backend="graphql").probe(
        _targets("acme/flaky"),
        kinds=("heads",),
    )
    git_report = AutoRefProbe(graphql, git, backend="git").probe(
        _targets("acme/flaky"),
        kinds=("heads",),
    )

    assert graphql_report.failures["acme/flaky"].kind == "transient"
    assert git_report.repos == git.report.repos
    assert git.calls == [(("acme/flaky",), ("heads",), "all")]


def _targets(*repos: str) -> list[ProbeTarget]:
    return [
        ProbeTarget(
            full_name=repo,
            default_branch="main",
            clone_url=f"https://github.com/{repo}.git",
        )
        for repo in repos
    ]


def _probed(branch: str, sha: str) -> ProbedRepo:
    return ProbedRepo(default_branch=branch, refs=(GitRef(kind="heads", name=branch, sha=sha),))
