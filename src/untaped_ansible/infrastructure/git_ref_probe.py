"""Git ls-remote backed remote ref freshness probe."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from untaped.api import bounded_map

from untaped_ansible.domain.errors import GitCacheError
from untaped_ansible.domain.payloads import (
    GitRef,
    ProbedRepo,
    ProbeFailure,
    ProbeReport,
    ProbeTarget,
)
from untaped_ansible.domain.repo_targets import remote_url_for

GIT_REF_PROBE_FAILURE_PREFIX = "git ref probe failed: "


class _LsRemoteGit(Protocol):
    def ls_remote(
        self,
        url: str,
        *,
        patterns: list[str],
        auth_header: str | None,
    ) -> str: ...


class GitRemoteRefProbe:
    """Probe branch/tag heads for many repos using ``git ls-remote``."""

    def __init__(
        self,
        git: _LsRemoteGit,
        *,
        clone_protocol: str,
        auth_header: str | None,
        concurrency: int = 8,
    ) -> None:
        if clone_protocol not in {"https", "ssh"}:
            raise ValueError("clone_protocol must be 'https' or 'ssh'")
        if concurrency < 1 or concurrency > 32:
            raise ValueError("concurrency must be between 1 and 32")
        self._git = git
        self._clone_protocol = clone_protocol
        self._auth_header = auth_header if clone_protocol == "https" else None
        self._concurrency = concurrency

    def probe(
        self,
        repos: Sequence[ProbeTarget],
        *,
        kinds: Sequence[str],
        mode: Literal["all", "default_branch"] = "all",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ProbeReport:
        if mode not in {"all", "default_branch"}:
            raise ValueError("mode must be 'all' or 'default_branch'")
        probed: dict[str, ProbedRepo] = {}
        failures: dict[str, ProbeFailure] = {}
        total = len(repos)
        done = 0

        def probe_one(target: ProbeTarget) -> ProbedRepo | ProbeFailure:
            return self._probe_one(target, kinds=kinds, mode=mode)

        def record(target: ProbeTarget, outcome: ProbedRepo | ProbeFailure) -> None:
            nonlocal done
            if isinstance(outcome, ProbeFailure):
                failures[target.full_name] = outcome
            else:
                probed[target.full_name] = outcome
            done += 1
            if on_progress is not None:
                on_progress(done, total)

        bounded_map(probe_one, repos, concurrency=self._concurrency, on_each=record)
        return ProbeReport(repos=probed, failures=failures)

    def _probe_one(
        self,
        target: ProbeTarget,
        *,
        kinds: Sequence[str],
        mode: Literal["all", "default_branch"],
    ) -> ProbedRepo | ProbeFailure:
        url = remote_url_for(target, self._clone_protocol)
        try:
            output = self._git.ls_remote(
                url,
                patterns=_patterns_for(target, kinds=kinds, mode=mode),
                auth_header=self._auth_header,
            )
        except GitCacheError as exc:
            reason = str(exc) or type(exc).__name__
            return ProbeFailure(kind="git", reason=f"{GIT_REF_PROBE_FAILURE_PREFIX}{reason}")
        return _parse_ls_remote(output, target=target, kinds=kinds, mode=mode)


def _patterns_for(
    target: ProbeTarget,
    *,
    kinds: Sequence[str],
    mode: Literal["all", "default_branch"],
) -> list[str]:
    patterns = ["HEAD"]
    if mode == "default_branch":
        if target.default_branch and target.default_branch != "HEAD":
            patterns.append(f"refs/heads/{target.default_branch}")
        return patterns
    kind_set = set(kinds)
    if "heads" in kind_set:
        patterns.append("refs/heads/*")
    if "tags" in kind_set:
        patterns.append("refs/tags/*")
    return patterns


def _parse_ls_remote(
    output: str,
    *,
    target: ProbeTarget,
    kinds: Sequence[str],
    mode: Literal["all", "default_branch"],
) -> ProbedRepo:
    symrefs: dict[str, str] = {}
    refs: dict[str, str] = {}
    peeled_tags: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("ref: "):
            value, _, name = line[5:].partition("\t")
            if value and name:
                symrefs[name] = value
            continue
        sha, separator, ref_name = line.partition("\t")
        if not separator or not sha or not ref_name:
            continue
        if ref_name.endswith("^{}"):
            peeled_tags[ref_name.removesuffix("^{}")] = sha
        else:
            refs[ref_name] = sha

    default_branch = _default_branch(symrefs, target)
    if mode == "default_branch":
        ref = _default_branch_ref(default_branch, refs)
        return ProbedRepo(default_branch=default_branch, refs=(ref,) if ref is not None else ())

    selected: list[GitRef] = []
    kind_set = set(kinds)
    if "heads" in kind_set:
        selected.extend(
            GitRef(kind="heads", name=name, sha=sha)
            for name, sha in _named_refs(refs, "refs/heads/")
        )
    if "tags" in kind_set:
        selected.extend(
            GitRef(kind="tags", name=name, sha=peeled_tags.get(full_ref, sha))
            for full_ref, name, sha in _named_refs_with_full_name(refs, "refs/tags/")
        )
    return ProbedRepo(
        default_branch=default_branch,
        refs=tuple(sorted(selected, key=lambda ref: (ref.kind, ref.name))),
    )


def _default_branch(symrefs: dict[str, str], target: ProbeTarget) -> str:
    head_target = symrefs.get("HEAD")
    if head_target and head_target.startswith("refs/heads/"):
        return head_target.removeprefix("refs/heads/")
    return target.default_branch


def _default_branch_ref(default_branch: str, refs: dict[str, str]) -> GitRef | None:
    full_ref = f"refs/heads/{default_branch}"
    sha = refs.get(full_ref) or refs.get("HEAD")
    if sha is None:
        return None
    return GitRef(kind="heads", name=default_branch, sha=sha)


def _named_refs(refs: dict[str, str], prefix: str) -> list[tuple[str, str]]:
    return [
        (full_ref.removeprefix(prefix), sha)
        for full_ref, sha in refs.items()
        if full_ref.startswith(prefix)
    ]


def _named_refs_with_full_name(refs: dict[str, str], prefix: str) -> list[tuple[str, str, str]]:
    return [
        (full_ref, full_ref.removeprefix(prefix), sha)
        for full_ref, sha in refs.items()
        if full_ref.startswith(prefix)
    ]
