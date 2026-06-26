"""GraphQL-backed remote ref freshness probe satisfying the RefProbe port."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from untaped.api import HttpError, UntapedError
from untaped_github import GithubGraphqlError

from untaped_ansible._concurrency import bounded_map
from untaped_ansible.domain.payloads import GitRef, ProbedRepo, ProbeReport

if TYPE_CHECKING:
    from untaped_github import BatchRepoRefsResult

GRAPHQL_CHUNK_SIZE = 50
_MISSING_REASON = "repository not found or inaccessible on GitHub"


class _BatchRepoRefsClient(Protocol):
    """The slice of ``untaped_github.GithubClient`` the probe needs."""

    def batch_repo_refs(
        self,
        repos: Sequence[str],
        *,
        kinds: Sequence[str] = ("heads", "tags"),
        chunk_size: int = 50,
    ) -> BatchRepoRefsResult: ...

    def batch_default_branch_refs(
        self,
        repos: Sequence[str],
        *,
        chunk_size: int = 200,
    ) -> BatchRepoRefsResult: ...


class GithubRefProbe:
    """Probe branch/tag heads for many repos via batched GraphQL queries.

    Splits ``repos`` into GraphQL-sized chunks and drives them through a
    bounded thread pool; each worker issues one ``batch_repo_refs`` call.
    Generic chunk-level transport errors mark every repo in that chunk as
    failed; classified global GraphQL failures abort the probe.
    """

    def __init__(
        self,
        github: _BatchRepoRefsClient,
        *,
        concurrency: int = 8,
        chunk_size: int = GRAPHQL_CHUNK_SIZE,
    ) -> None:
        if concurrency < 1 or concurrency > 32:
            raise ValueError("concurrency must be between 1 and 32")
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._github = github
        self._concurrency = concurrency
        self._chunk_size = chunk_size

    def probe(
        self,
        repos: Sequence[str],
        *,
        kinds: Sequence[str],
        mode: Literal["all", "default_branch"] = "all",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ProbeReport:
        if mode not in {"all", "default_branch"}:
            raise ValueError("mode must be 'all' or 'default_branch'")
        total = len(repos)
        chunks = [
            tuple(repos[start : start + self._chunk_size])
            for start in range(0, total, self._chunk_size)
        ]
        probed: dict[str, ProbedRepo] = {}
        failures: dict[str, str] = {}
        rate_limit_cost: int | None = None
        rate_limit_remaining: int | None = None
        rate_limit_reset_at = None
        done = 0

        def merge(chunk: tuple[str, ...], outcome: BatchRepoRefsResult | str) -> None:
            nonlocal rate_limit_cost, rate_limit_remaining, rate_limit_reset_at
            if isinstance(outcome, str):
                failures.update(dict.fromkeys(chunk, outcome))
                return
            for repo_refs in outcome.repos:
                probed[repo_refs.full_name] = ProbedRepo(
                    default_branch=repo_refs.default_branch,
                    refs=tuple(
                        GitRef(kind=ref.kind, name=ref.name, sha=ref.sha) for ref in repo_refs.refs
                    ),
                )
            failures.update(dict.fromkeys(outcome.missing, _MISSING_REASON))
            if outcome.rate_limit_remaining is not None:
                rate_limit_remaining = (
                    outcome.rate_limit_remaining
                    if rate_limit_remaining is None
                    else min(rate_limit_remaining, outcome.rate_limit_remaining)
                )
            cost = getattr(outcome, "rate_limit_cost", None)
            if cost is not None:
                rate_limit_cost = cost if rate_limit_cost is None else rate_limit_cost + cost
            reset_at = getattr(outcome, "rate_limit_reset_at", None)
            if reset_at is not None:
                rate_limit_reset_at = reset_at

        def probe_chunk(chunk: tuple[str, ...]) -> BatchRepoRefsResult | str:
            return self._probe_chunk(chunk, kinds, mode=mode)

        def record(chunk: tuple[str, ...], outcome: BatchRepoRefsResult | str) -> None:
            nonlocal done
            merge(chunk, outcome)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done, total)

        bounded_map(probe_chunk, chunks, concurrency=self._concurrency, on_each=record)
        return ProbeReport(
            repos=probed,
            failures=failures,
            rate_limit_cost=rate_limit_cost,
            rate_limit_remaining=rate_limit_remaining,
            rate_limit_reset_at=rate_limit_reset_at,
        )

    def _probe_chunk(
        self,
        chunk: tuple[str, ...],
        kinds: Sequence[str],
        *,
        mode: Literal["all", "default_branch"],
    ) -> BatchRepoRefsResult | str:
        try:
            if mode == "default_branch":
                return self._github.batch_default_branch_refs(chunk, chunk_size=len(chunk))
            return self._github.batch_repo_refs(chunk, kinds=kinds, chunk_size=len(chunk))
        except GithubGraphqlError:
            # Must precede the broad UntapedError catch: this is a global
            # GraphQL failure, not a per-repo chunk failure.
            raise
        except (HttpError, UntapedError) as exc:
            # Provenance prefix: distinguishes probe transport failures from
            # git-fetch failures in `failed <repo>: <reason>` stderr listings.
            return f"ref probe failed: {str(exc) or type(exc).__name__}"
