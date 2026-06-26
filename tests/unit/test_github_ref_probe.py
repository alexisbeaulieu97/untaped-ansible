"""Tests for the GraphQL-backed ref freshness probe adapter."""

from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest
from untaped.api import HttpError, UntapedError
from untaped_github import BatchRepoRefsResult, GithubGraphqlError, RepoRef, RepoRefs

from untaped_ansible.domain.payloads import GitRef
from untaped_ansible.infrastructure.github_ref_probe import GithubRefProbe


class FakeBatchClient:
    """Configurable batch_repo_refs stub keyed by repo full name."""

    def __init__(self) -> None:
        self.refs: dict[str, list[RepoRef]] = {}
        self.default_branches: dict[str, str | None] = {}
        self.missing: set[str] = set()
        self.errors: dict[str, Exception] = {}
        self.rate_limits: list[int | None] = []
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...], int]] = []
        self.default_branch_calls: list[tuple[tuple[str, ...], int]] = []
        self._lock = threading.Lock()

    def batch_repo_refs(
        self,
        repos: Sequence[str],
        *,
        kinds: Sequence[str] = ("heads", "tags"),
        chunk_size: int = 50,
    ) -> BatchRepoRefsResult:
        with self._lock:
            self.calls.append((tuple(repos), tuple(kinds), chunk_size))
            rate_limit = self.rate_limits.pop(0) if self.rate_limits else None
        found: list[RepoRefs] = []
        missing: list[str] = []
        for repo in repos:
            error = self.errors.get(repo)
            if error is not None:
                raise error
            if repo in self.missing:
                missing.append(repo)
                continue
            found.append(
                RepoRefs(
                    full_name=repo,
                    default_branch=self.default_branches.get(repo, "main"),
                    refs=tuple(ref for ref in self.refs.get(repo, []) if ref.kind in kinds),
                )
            )
        return BatchRepoRefsResult(
            repos=tuple(found),
            missing=tuple(missing),
            rate_limit_remaining=rate_limit,
        )

    def batch_default_branch_refs(
        self,
        repos: Sequence[str],
        *,
        chunk_size: int = 200,
    ) -> BatchRepoRefsResult:
        with self._lock:
            self.default_branch_calls.append((tuple(repos), chunk_size))
            rate_limit = self.rate_limits.pop(0) if self.rate_limits else None
        found: list[RepoRefs] = []
        missing: list[str] = []
        for repo in repos:
            error = self.errors.get(repo)
            if error is not None:
                raise error
            if repo in self.missing:
                missing.append(repo)
                continue
            branch = self.default_branches.get(repo, "main")
            refs = ()
            if branch is not None:
                refs = (RepoRef(kind="heads", name=branch, sha=f"sha-{repo}-{branch}"),)
            found.append(
                RepoRefs(
                    full_name=repo,
                    default_branch=branch,
                    refs=refs,
                )
            )
        return BatchRepoRefsResult(
            repos=tuple(found),
            missing=tuple(missing),
            rate_limit_remaining=rate_limit,
        )


def test_probe_converts_batch_results_to_domain_refs() -> None:
    client = FakeBatchClient()
    client.refs["acme/site"] = [
        RepoRef(kind="heads", name="main", sha="sha-main"),
        RepoRef(kind="tags", name="v1", sha="sha-v1"),
    ]
    client.default_branches["acme/site"] = "main"
    client.missing.add("acme/gone")

    report = GithubRefProbe(client).probe(["acme/site", "acme/gone"], kinds=("heads", "tags"))

    assert report.repos["acme/site"].default_branch == "main"
    assert report.repos["acme/site"].refs == (
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="tags", name="v1", sha="sha-v1"),
    )
    assert report.failures == {"acme/gone": "repository not found or inaccessible on GitHub"}
    assert client.calls == [(("acme/site", "acme/gone"), ("heads", "tags"), 2)]


def test_probe_chunks_repos_and_reports_cumulative_progress() -> None:
    client = FakeBatchClient()
    repos = [f"acme/repo-{index}" for index in range(5)]
    for repo in repos:
        client.refs[repo] = [RepoRef(kind="heads", name="main", sha=f"sha-{repo}")]
    progress: list[tuple[int, int]] = []

    report = GithubRefProbe(client, concurrency=1, chunk_size=2).probe(
        repos,
        kinds=("heads",),
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert sorted(len(call[0]) for call in client.calls) == [1, 2, 2]
    assert set(report.repos) == set(repos)
    assert progress == [(2, 5), (4, 5), (5, 5)]


def test_probe_takes_min_rate_limit_across_chunks() -> None:
    client = FakeBatchClient()
    repos = [f"acme/repo-{index}" for index in range(4)]
    for repo in repos:
        client.refs[repo] = []
    client.rate_limits = [4800, 4500, None, 4900]

    report = GithubRefProbe(client, concurrency=1, chunk_size=1).probe(repos, kinds=("heads",))

    assert report.rate_limit_remaining == 4500


def test_probe_can_use_default_branch_ref_query() -> None:
    client = FakeBatchClient()
    client.default_branches["acme/site"] = "trunk"

    report = GithubRefProbe(client).probe(
        ["acme/site"],
        kinds=("heads", "tags"),
        mode="default_branch",
    )

    assert report.repos["acme/site"].default_branch == "trunk"
    assert report.repos["acme/site"].refs == (
        GitRef(kind="heads", name="trunk", sha="sha-acme/site-trunk"),
    )
    assert client.calls == []
    assert client.default_branch_calls == [(("acme/site",), 1)]


def test_probe_marks_failed_chunks_without_aborting_others() -> None:
    client = FakeBatchClient()
    client.refs["acme/ok"] = [RepoRef(kind="heads", name="main", sha="sha-ok")]
    client.errors["acme/boom"] = HttpError("github graphql 502", url="https://api.github.com")
    client.refs["acme/also-ok"] = [RepoRef(kind="heads", name="main", sha="sha-also")]

    report = GithubRefProbe(client, concurrency=2, chunk_size=1).probe(
        ["acme/ok", "acme/boom", "acme/also-ok"],
        kinds=("heads",),
    )

    assert set(report.repos) == {"acme/ok", "acme/also-ok"}
    assert "acme/boom" in report.failures
    assert "502" in report.failures["acme/boom"]
    assert report.failures["acme/boom"].startswith("ref probe failed: ")


def test_probe_marks_untaped_error_chunks_as_failures() -> None:
    client = FakeBatchClient()
    client.errors["acme/bad"] = UntapedError("invalid repository 'acme/bad'")

    report = GithubRefProbe(client).probe(["acme/bad"], kinds=("heads",))

    assert report.repos == {}
    assert report.failures == {"acme/bad": "ref probe failed: invalid repository 'acme/bad'"}


def test_probe_propagates_global_graphql_errors() -> None:
    client = FakeBatchClient()
    client.errors["acme/limited"] = GithubGraphqlError(
        "github graphql rate limit exceeded: API rate limit exceeded",
        kind="rate_limited",
        status_code=403,
        url="https://api.github.com/graphql",
    )

    with pytest.raises(GithubGraphqlError) as exc_info:
        GithubRefProbe(client).probe(["acme/limited"], kinds=("heads",))

    assert exc_info.value.kind == "rate_limited"


def test_probe_validates_construction_arguments() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        GithubRefProbe(FakeBatchClient(), concurrency=0)
    with pytest.raises(ValueError, match="chunk_size"):
        GithubRefProbe(FakeBatchClient(), chunk_size=0)


def test_probe_of_no_repos_returns_empty_report() -> None:
    report = GithubRefProbe(FakeBatchClient()).probe([], kinds=("heads",))

    assert report.repos == {}
    assert report.failures == {}
    assert report.rate_limit_remaining is None
