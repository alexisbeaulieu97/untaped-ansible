"""Tests for refreshing dependency sources through a local Git cache."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from untaped.api import UntapedError

from untaped_ansible.application.refresh_git_index import (
    RefreshGitSourceIndex,
    _repo_candidate,
    _source_refresh_fingerprint,
)
from untaped_ansible.domain.payloads import (
    CachedRef,
    GitRef,
    ProbedRepo,
    ProbeFailure,
    ProbeReport,
    ProbeTarget,
    RefreshProgressEvent,
    RefScan,
    SourceRepoMetadata,
)
from untaped_ansible.infrastructure.git_cache import GitCacheError
from untaped_ansible.infrastructure.sqlite_index import SqliteDependencyIndex
from untaped_ansible.settings import SourceDefinition


class FakeGitHub:
    def __init__(self) -> None:
        self.repository_calls: list[tuple[str, str]] = []
        self.org_calls: list[str] = []
        self.team_calls: list[tuple[str, str]] = []
        self.repo_default_branches: dict[str, str] = {}
        self.org_repos: dict[str, list[str]] = {}
        self.team_repos: dict[str, list[str]] = {}
        self.org_error: Exception | None = None

    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        self.repository_calls.append((owner, repo))
        full_name = f"{owner}/{repo}"
        return {
            "full_name": full_name,
            "default_branch": self.repo_default_branches.get(full_name, "main"),
            "clone_url": f"https://github.com/{full_name}.git",
            "ssh_url": f"git@github.com:{full_name}.git",
        }

    def list_org_repos(self, org: str) -> list[dict[str, object]]:
        self.org_calls.append(org)
        if self.org_error is not None:
            raise self.org_error
        return [self._row(name) for name in self.org_repos.get(org, [f"{org}/site"])]

    def list_team_repos(self, org: str, team_slug: str) -> list[dict[str, object]]:
        self.team_calls.append((org, team_slug))
        return [self._row(name) for name in self.team_repos.get(f"{org}/{team_slug}", [])]

    def _row(self, full_name: str) -> dict[str, object]:
        return {
            "full_name": full_name,
            "default_branch": "main",
            "clone_url": f"https://github.com/{full_name}.git",
            "ssh_url": f"git@github.com:{full_name}.git",
        }

    def list_matching_refs(self, owner: str, repo: str, namespace: str) -> list[dict[str, object]]:
        raise AssertionError("git cache refresh must not use REST matching-refs")

    def get_tree(
        self,
        owner: str,
        repo: str,
        tree_sha: str,
        *,
        recursive: bool = False,
    ) -> dict[str, object]:
        raise AssertionError("git cache refresh must not use REST tree reads")

    def get_raw_content(self, owner: str, repo: str, path: str, *, ref: str) -> str:
        raise AssertionError("git cache refresh must not use REST content reads")


class FakeRefProbe:
    """Dict-backed RefProbe: refs and failures keyed by full repo name."""

    def __init__(self) -> None:
        self.refs: dict[str, list[GitRef]] = {}
        self.default_branches: dict[str, str | None] = {}
        self.failures: dict[str, str | ProbeFailure] = {}
        self.rate_limit_remaining: int | None = None
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []

    def probe(
        self,
        repos: Sequence[ProbeTarget],
        *,
        kinds: Sequence[str],
        mode: str = "all",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ProbeReport:
        names = tuple(repo.full_name for repo in repos)
        self.calls.append((names, tuple(kinds), mode))
        probed: dict[str, ProbedRepo] = {}
        failures: dict[str, ProbeFailure] = {}
        for repo in names:
            if repo in self.failures:
                failure = self.failures[repo]
                failures[repo] = (
                    failure
                    if isinstance(failure, ProbeFailure)
                    else ProbeFailure(kind="chunk", reason=failure)
                )
                continue
            if repo not in self.refs:
                failures[repo] = ProbeFailure(
                    kind="missing",
                    reason="repository not found or inaccessible on GitHub",
                )
                continue
            probed[repo] = ProbedRepo(
                default_branch=self.default_branches.get(repo, "main"),
                refs=tuple(ref for ref in self.refs[repo] if ref.kind in kinds),
            )
        if on_progress is not None:
            on_progress(len(names), len(names))
        return ProbeReport(
            repos=probed,
            failures=failures,
            rate_limit_remaining=self.rate_limit_remaining,
        )


class FakeGitCache:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str, str], str] = {}
        self.ensure_calls: list[tuple[str, Path, str | None]] = []
        self.fetches: list[tuple[str, tuple[str, ...], int, bool, str | None]] = []
        self.reads: list[tuple[str, str, str, str | None]] = []
        self.fail_fetches: set[str] = set()

    def ensure_bare(
        self,
        url: str,
        *,
        cache_dir: Path,
        auth_header: str | None,
    ) -> Path:
        self.ensure_calls.append((url, cache_dir, auth_header))
        return cache_dir / url.removesuffix(".git").rsplit("/", maxsplit=1)[-1]

    def fetch_refs(
        self,
        bare_path: Path,
        *,
        refspecs: list[str],
        depth: int,
        blob_filter: bool,
        auth_header: str | None,
    ) -> None:
        if bare_path.name in self.fail_fetches:
            raise GitCacheError(f"git fetch failed for {bare_path.name}")
        self.fetches.append((bare_path.name, tuple(refspecs), depth, blob_filter, auth_header))

    def read_file(
        self,
        bare_path: Path,
        sha: str,
        path: str,
        *,
        auth_header: str | None,
    ) -> str | None:
        self.reads.append((bare_path.name, sha, path, auth_header))
        return self.files.get((bare_path.name, sha, path))


class SlowGitCache(FakeGitCache):
    def __init__(self) -> None:
        super().__init__()
        self.active_fetches = 0
        self.max_active_fetches = 0
        self._lock = threading.Lock()

    def fetch_refs(
        self,
        bare_path: Path,
        *,
        refspecs: list[str],
        depth: int,
        blob_filter: bool,
        auth_header: str | None,
    ) -> None:
        with self._lock:
            self.active_fetches += 1
            self.max_active_fetches = max(self.max_active_fetches, self.active_fetches)
        try:
            time.sleep(0.05)
            super().fetch_refs(
                bare_path,
                refspecs=refspecs,
                depth=depth,
                blob_filter=blob_filter,
                auth_header=auth_header,
            )
        finally:
            with self._lock:
                self.active_fetches -= 1


class SlowRefScanIndex:
    def __init__(self, wrapped: SqliteDependencyIndex) -> None:
        self._wrapped = wrapped
        self.active_ref_scans = 0
        self.max_active_ref_scans = 0
        self._lock = threading.Lock()

    def status(self, source_key: str):
        return self._wrapped.status(source_key)

    def ref_scans(self, source_key: str, source_repo: str, refs):
        with self._lock:
            self.active_ref_scans += 1
            self.max_active_ref_scans = max(self.max_active_ref_scans, self.active_ref_scans)
        try:
            time.sleep(0.05)
            return self._wrapped.ref_scans(source_key, source_repo, refs)
        finally:
            with self._lock:
                self.active_ref_scans -= 1

    def commit_source_ref_refresh(self, source_key: str, **kwargs):
        return self._wrapped.commit_source_ref_refresh(source_key, **kwargs)

    def commit_source_ref_partial_refresh(self, source_key: str, **kwargs):
        return self._wrapped.commit_source_ref_partial_refresh(source_key, **kwargs)

    def complete_source_ref_refresh(self, source_key: str, **kwargs):
        return self._wrapped.complete_source_ref_refresh(source_key, **kwargs)

    def refresh_progress(self, source_key: str, source_fingerprint: str):
        return self._wrapped.refresh_progress(source_key, source_fingerprint)

    def clear_refresh_progress(self, source_key: str):
        return self._wrapped.clear_refresh_progress(source_key)


class CountingRefScanIndex:
    def __init__(self, wrapped: SqliteDependencyIndex) -> None:
        self._wrapped = wrapped
        self.ref_scans_calls: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def status(self, source_key: str):
        return self._wrapped.status(source_key)

    def ref_scans(self, source_key: str, source_repo: str, refs):
        refs_tuple = tuple(refs)
        self.ref_scans_calls.append((source_key, source_repo, refs_tuple))
        return self._wrapped.ref_scans(source_key, source_repo, refs_tuple)

    def commit_source_ref_refresh(self, source_key: str, **kwargs):
        return self._wrapped.commit_source_ref_refresh(source_key, **kwargs)

    def commit_source_ref_partial_refresh(self, source_key: str, **kwargs):
        return self._wrapped.commit_source_ref_partial_refresh(source_key, **kwargs)

    def complete_source_ref_refresh(self, source_key: str, **kwargs):
        return self._wrapped.complete_source_ref_refresh(source_key, **kwargs)

    def refresh_progress(self, source_key: str, source_fingerprint: str):
        return self._wrapped.refresh_progress(source_key, source_fingerprint)

    def clear_refresh_progress(self, source_key: str):
        return self._wrapped.clear_refresh_progress(source_key)


def _make_refresh(
    *,
    github: FakeGitHub,
    git: FakeGitCache,
    probe: FakeRefProbe,
    index: Any,
    tmp_path: Path,
    **overrides: Any,
) -> RefreshGitSourceIndex:
    kwargs: dict[str, Any] = {
        "github": github,
        "git": git,
        "probe": probe,
        "index": index,
        "aliases": {},
        "default_dependency_paths": ["roles/requirements.yml"],
        "repo_cache_path": tmp_path / "repos",
        "clone_protocol": "https",
        "fetch_depth": 1,
        "blob_filter": True,
        "auth_header": None,
    }
    kwargs.update(overrides)
    return RefreshGitSourceIndex(**kwargs)


def test_pruning_is_scoped_to_succeeded_repos(tmp_path: Path) -> None:
    """A failed repo keeps its cached refs; a succeeded repo prunes removed refs."""
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [
        GitRef(kind="heads", name="main", sha="sha-a-main"),
        GitRef(kind="heads", name="extra", sha="sha-a-extra"),
    ]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b-main")]
    git.files[("a", "sha-a-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    git.files[("a", "sha-a-extra", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/extra-base\n"
    )
    git.files[("b", "sha-b-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b"])

    first = refresh(source, source_key="source:prod")
    assert first.failures == ()

    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a-main")]
    probe.failures["acme/b"] = "boom"
    second = refresh(source, source_key="source:prod")

    assert [(failure.repo, failure.reason) for failure in second.failures] == [("acme/b", "boom")]
    # succeeded repo: the removed ref is pruned
    assert index.ref_scans("source:prod", "acme/a", [("heads", "extra")]) == {}
    assert not index.dependents("acme/extra-base", None, source_key="source:prod")
    # failed repo: previously cached refs survive
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")])
    assert {
        edge.source_repo for edge in index.dependents("acme/base", "v1", source_key="source:prod")
    } == {"acme/a", "acme/b"}
    # failed repo keeps its cached default-branch metadata too
    assert CachedRef(name="main", kind="heads", default_branch="main") in set(
        index.cached_ref_metadata("acme/b", source_key="source:prod")
    )


def test_probe_failure_skips_repo_without_git_work_and_records_failure(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    probe.failures["acme/gone"] = "repository not found or inaccessible on GitHub"
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)(
        SourceDefinition(name="prod", repos=["acme/gone", "acme/site"]),
        source_key="source:prod",
    )

    assert result.repos == 2
    assert result.refs == 1
    assert [(failure.repo, failure.reason) for failure in result.failures] == [
        ("acme/gone", "repository not found or inaccessible on GitHub")
    ]
    assert all("gone" not in url for url, _, _ in git.ensure_calls)
    assert index.dependents("acme/base", None, source_key="source:prod")


def test_parse_warnings_are_reported_as_skipped_files_without_failing_repo(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "---\ngalaxy_info:\n  role_name: {@ role_slug @}\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)(
        SourceDefinition(name="prod", repos=["acme/site"]),
        source_key="source:prod",
    )

    assert result.failures == ()
    assert [
        (skipped.repo, skipped.ref, skipped.source_path, skipped.reason)
        for skipped in result.skipped_files
    ] == [
        (
            "acme/site",
            "main",
            "roles/requirements.yml",
            "could not parse dependency YAML",
        )
    ]


def test_fetch_failure_keeps_other_repos_and_previously_cached_refs(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    git.files[("b", "sha-b", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b"])

    refresh(source, source_key="source:prod")
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a2")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b2")]
    git.files[("a", "sha-a2", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v2\n"
    )
    git.files[("b", "sha-b2", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v2\n"
    )
    git.fail_fetches.add("b")

    result = refresh(source, source_key="source:prod")

    assert [(failure.repo, failure.reason) for failure in result.failures] == [
        ("acme/b", "git fetch failed for b")
    ]
    assert result.changed_refs == 1
    # the succeeded repo advanced to v2
    assert {
        edge.source_repo for edge in index.dependents("acme/base", "v2", source_key="source:prod")
    } == {"acme/a"}
    # the failed repo keeps its previously cached v1 scan
    assert {
        edge.source_repo for edge in index.dependents("acme/base", "v1", source_key="source:prod")
    } == {"acme/b"}


def test_all_repos_failed_skips_commit_and_keeps_index_untouched(tmp_path: Path) -> None:
    """When every repo fails (probe or fetch), the run must not look fresh."""
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    git.files[("b", "sha-b", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b"])

    refresh(source, source_key="source:prod")
    before = index.status("source:prod")
    assert before is not None
    probe.failures["acme/a"] = "probe boom"
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b2")]
    git.fail_fetches.add("b")

    result = refresh(source, source_key="source:prod")

    assert [(failure.repo, failure.reason) for failure in result.failures] == [
        ("acme/a", "probe boom"),
        ("acme/b", "git fetch failed for b"),
    ]
    after = index.status("source:prod")
    assert after is not None
    assert after.scanned_at == before.scanned_at
    # all previously cached data survives untouched
    assert index.ref_scans("source:prod", "acme/a", [("heads", "main")])
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")])
    assert {
        edge.source_repo for edge in index.dependents("acme/base", "v1", source_key="source:prod")
    } == {"acme/a", "acme/b"}


def test_empty_source_refresh_still_commits(tmp_path: Path) -> None:
    """Zero repos expanded is a successful (empty) refresh, not a failure."""
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)
    source = SourceDefinition(name="prod", orgs=["acme"])

    refresh(source, source_key="source:prod")
    github.org_repos["acme"] = []
    result = refresh(source, source_key="source:prod")

    assert result.repos == 0
    assert result.failures == ()
    # the commit ran: the now-unselected repo was pruned
    assert index.ref_scans("source:prod", "acme/site", [("heads", "main")]) == {}
    assert not index.dependents("acme/base", None, source_key="source:prod")


def test_git_refresh_fetches_selected_refs_and_indexes_dependency_files(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        auth_header="AUTHORIZATION: bearer test",
        ref_scan_default="default_branch",
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert result.repos == 1
    assert result.refs == 1
    assert result.edges == 1
    assert probe.calls == [(("acme/site",), ("heads",), "default_branch")]
    assert git.fetches == [
        (
            "site",
            ("+refs/heads/main:refs/heads/main",),
            1,
            True,
            "AUTHORIZATION: bearer test",
        )
    ]
    assert git.reads == [
        ("site", "sha-main", "roles/requirements.yml", "AUTHORIZATION: bearer test")
    ]
    assert index.dependents("acme/base", "v1", source_key="source:prod")[0].source_repo == (
        "acme/site"
    )


def test_git_refresh_defaults_to_all_heads_and_tags(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="heads", name="master", sha="sha-master"),
        GitRef(kind="tags", name="v3", sha="sha-v3"),
    ]
    git.files[("site", "sha-master", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v3\n"
    )
    git.files[("site", "sha-v3", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v3\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        ref_scan_default="all",
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert result.refs == 2
    assert probe.calls == [(("acme/site",), ("heads", "tags"), "all")]
    assert git.fetches == [
        (
            "site",
            (
                "+refs/heads/master:refs/heads/master",
                "+refs/tags/v3:refs/tags/v3",
            ),
            1,
            True,
            None,
        )
    ]
    assert [
        edge.source_ref for edge in index.dependents("acme/base", "v3", source_key="source:prod")
    ] == ["master", "v3"]
    assert set(index.cached_ref_metadata("acme/site", source_key="source:prod")) == {
        CachedRef(name="master", kind="heads", default_branch="main"),
        CachedRef(name="v3", kind="tags", default_branch="main"),
    }


def test_probe_kinds_follow_explicit_ref_kinds(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="tags", name="v1", sha="sha-v1")]
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)(
        SourceDefinition(name="prod", orgs=["acme"], ref_kinds=["tags"]),
        source_key="source:prod",
    )

    assert probe.calls == [(("acme/site",), ("tags",), "all")]


def test_source_ref_scan_default_overrides_global_default(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="tags", name="v1", sha="sha-v1"),
    ]
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        ref_scan_default="all",
    )(
        SourceDefinition(
            name="prod",
            orgs=["acme"],
            ref_scan_default="default_branch",
        ),
        source_key="source:prod",
    )

    assert probe.calls == [(("acme/site",), ("heads",), "default_branch")]


def test_default_branch_probe_is_not_used_with_explicit_ref_patterns(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="heads", name="release", sha="sha-release"),
    ]
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        ref_scan_default="default_branch",
    )(
        SourceDefinition(name="prod", orgs=["acme"], ref_patterns=["release"]),
        source_key="source:prod",
    )

    assert probe.calls == [(("acme/site",), ("heads", "tags"), "all")]


def test_default_branch_comes_from_probe_with_expansion_fallback(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="trunk", sha="sha-trunk")]
    probe.default_branches["acme/site"] = "trunk"
    probe.refs["acme/old"] = [GitRef(kind="heads", name="main", sha="sha-old")]
    probe.default_branches["acme/old"] = None
    github.org_repos["acme"] = ["acme/site", "acme/old"]
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)(
        SourceDefinition(name="prod", orgs=["acme"]),
        source_key="source:prod",
    )

    assert set(index.cached_ref_metadata("acme/site", source_key="source:prod")) == {
        CachedRef(name="trunk", kind="heads", default_branch="trunk"),
    }
    # probe did not know the default branch: expansion metadata wins
    assert set(index.cached_ref_metadata("acme/old", source_key="source:prod")) == {
        CachedRef(name="main", kind="heads", default_branch="main"),
    }


def test_expansion_dedupes_overlapping_selectors_with_explicit_repo_precedence(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.repo_default_branches["acme/site"] = "trunk"
    github.org_repos["acme"] = ["acme/site", "acme/lib"]
    github.team_repos["acme/platform"] = ["acme/lib", "acme/tool"]
    git = FakeGitCache()
    probe = FakeRefProbe()
    for repo in ("acme/site", "acme/lib", "acme/tool"):
        probe.refs[repo] = [GitRef(kind="heads", name="main", sha=f"sha-{repo.split('/')[1]}")]
    probe.default_branches["acme/site"] = None
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
    )(
        SourceDefinition(
            name="prod",
            repos=["acme/site"],
            orgs=["acme"],
            teams=["platform"],
        ),
        source_key="source:prod",
    )

    assert result.repos == 3
    # repo list is deduped and deterministically sorted
    assert probe.calls[0][0] == ("acme/lib", "acme/site", "acme/tool")
    # explicit repo expansion metadata wins over the org listing
    assert set(index.cached_ref_metadata("acme/site", source_key="source:prod")) == {
        CachedRef(name="main", kind="heads", default_branch="trunk"),
    }


def test_expansion_failures_stay_fatal(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.org_error = UntapedError("org not found: acme")
    probe = FakeRefProbe()

    refresh = _make_refresh(
        github=github,
        git=FakeGitCache(),
        probe=probe,
        index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
        tmp_path=tmp_path,
    )

    with pytest.raises(UntapedError, match="org not found"):
        refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")
    assert probe.calls == []


def test_refresh_reports_progress_events_per_phase(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    events: list[RefreshProgressEvent] = []

    _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
        tmp_path=tmp_path,
        on_progress=events.append,
    )(SourceDefinition(name="prod", repos=["acme/a", "acme/b"]), source_key="source:prod")

    expanding = [event for event in events if event.phase == "expanding"]
    probing = [event for event in events if event.phase == "probing"]
    fetching = [event for event in events if event.phase == "fetching"]
    assert expanding and expanding[-1].done == expanding[-1].total == 2
    assert probing == [RefreshProgressEvent(phase="probing", done=2, total=2)]
    assert [event.done for event in fetching] == [1, 2]
    assert fetching[-1].changed == 2


def test_refresh_surfaces_probe_rate_limit(tmp_path: Path) -> None:
    github = FakeGitHub()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    probe.rate_limit_remaining = 240

    result = _make_refresh(
        github=github,
        git=FakeGitCache(),
        probe=probe,
        index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
        tmp_path=tmp_path,
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert result.rate_limit_remaining == 240


def test_refresh_pauses_on_low_graphql_budget_and_resumes_remaining_repos(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = "- src: https://github.com/acme/base-a\n"
    git.files[("b", "sha-b", "roles/requirements.yml")] = "- src: https://github.com/acme/base-b\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        repo_batch_size=1,
        rate_limit_floor=500,
    )
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b"])
    probe.rate_limit_remaining = 200

    first = refresh(source, source_key="source:prod")

    assert first.completed is False
    assert first.pause_reason == "GitHub GraphQL rate limit is low: 200 points remaining"
    assert first.failures == ()
    assert index.status("source:prod") is None
    assert index.ref_scans("source:prod", "acme/a", [("heads", "main")])
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")]) == {}

    probe.rate_limit_remaining = 1200
    second = refresh(source, source_key="source:prod")

    assert second.completed is True
    assert second.failures == ()
    assert second.refs == 2
    assert probe.calls == [
        (("acme/a",), ("heads", "tags"), "all"),
        (("acme/b",), ("heads", "tags"), "all"),
    ]
    status = index.status("source:prod")
    assert status is not None
    assert status.repos == 2
    assert status.refs == 2


def test_refresh_retries_failed_repos_after_budget_pause_without_double_probe(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    for repo, sha in {
        "acme/a": "sha-a",
        "acme/b": "sha-b",
        "acme/c": "sha-c",
    }.items():
        name = repo.split("/", maxsplit=1)[1]
        probe.refs[repo] = [GitRef(kind="heads", name="main", sha=sha)]
        git.files[(name, sha, "roles/requirements.yml")] = f"- src: https://github.com/{repo}\n"
    probe.failures["acme/b"] = "temporary probe failure"
    probe.rate_limit_remaining = 200
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        repo_batch_size=2,
        rate_limit_floor=500,
    )
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b", "acme/c"])

    first = refresh(source, source_key="source:prod")

    assert first.completed is False
    assert [(failure.repo, failure.reason) for failure in first.failures] == [
        ("acme/b", "temporary probe failure")
    ]
    assert index.ref_scans("source:prod", "acme/a", [("heads", "main")])
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")]) == {}
    assert index.ref_scans("source:prod", "acme/c", [("heads", "main")]) == {}

    del probe.failures["acme/b"]
    probe.rate_limit_remaining = 1200
    second = refresh(source, source_key="source:prod")

    assert second.completed is True
    assert second.failures == ()
    assert second.refs == 3
    assert probe.calls == [
        (("acme/a", "acme/b"), ("heads", "tags"), "all"),
        (("acme/b", "acme/c"), ("heads", "tags"), "all"),
    ]
    assert index.ref_scans("source:prod", "acme/a", [("heads", "main")])
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")])
    assert index.ref_scans("source:prod", "acme/c", [("heads", "main")])


def test_resumed_refresh_can_complete_with_later_failures_after_prior_success(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = "- src: https://github.com/acme/a\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        repo_batch_size=1,
        rate_limit_floor=500,
    )
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b"])
    probe.rate_limit_remaining = 200

    first = refresh(source, source_key="source:prod")

    assert first.completed is False
    probe.rate_limit_remaining = 1200
    probe.failures["acme/b"] = "temporary probe failure"
    second = refresh(source, source_key="source:prod")

    assert second.completed is True
    assert [(failure.repo, failure.reason) for failure in second.failures] == [
        ("acme/b", "temporary probe failure")
    ]
    assert probe.calls == [
        (("acme/a",), ("heads", "tags"), "all"),
        (("acme/b",), ("heads", "tags"), "all"),
    ]
    status = index.status("source:prod")
    assert status is not None
    assert status.refs == 1


def test_source_refresh_fingerprint_is_order_invariant_for_repos() -> None:
    source = SourceDefinition(name="prod", orgs=["acme"])
    repos = [
        _repo_candidate({"full_name": "acme/a"}, fallback=None),
        _repo_candidate({"full_name": "acme/b"}, fallback=None),
    ]

    first = _source_refresh_fingerprint(
        source,
        repos=repos,
        paths_fingerprint="paths",
        aliases_fingerprint="aliases",
        ref_scan_default="all",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
    )
    second = _source_refresh_fingerprint(
        source,
        repos=list(reversed(repos)),
        paths_fingerprint="paths",
        aliases_fingerprint="aliases",
        ref_scan_default="all",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
    )

    assert first == second


def test_partial_refresh_commit_persists_progress_in_same_adapter_call(
    tmp_path: Path,
) -> None:
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    checked_at = datetime.now(UTC)
    scan = RefScan(
        source_key="source:prod",
        source_repo="acme/a",
        ref_kind="heads",
        source_ref="main",
        source_sha="sha-a",
        clone_url="https://github.com/acme/a.git",
        clone_protocol="https",
        dependency_paths_fingerprint="paths",
        aliases_fingerprint="aliases",
        checked_at=checked_at,
        indexed_at=checked_at,
        dependencies=(),
    )

    index.commit_source_ref_partial_refresh(
        "source:prod",
        scans=(scan,),
        touches=(),
        keep={("acme/a", "heads", "main")},
        repo_metadata=(
            SourceRepoMetadata(
                source_key="source:prod",
                source_repo="acme/a",
                default_branch="main",
            ),
        ),
        processed_repos=frozenset({"acme/a"}),
        source_fingerprint="fingerprint",
        progress_statuses={"acme/a": "success"},
    )

    assert index.ref_scans("source:prod", "acme/a", [("heads", "main")])
    assert index.refresh_progress("source:prod", "fingerprint") == {"acme/a": "success"}


def test_partial_refresh_prunes_removed_repos_only_after_completion(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    probe.refs["acme/c"] = [GitRef(kind="heads", name="main", sha="sha-c")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = "- src: https://github.com/acme/base-a\n"
    git.files[("b", "sha-b", "roles/requirements.yml")] = "- src: https://github.com/acme/base-b\n"
    git.files[("c", "sha-c", "roles/requirements.yml")] = "- src: https://github.com/acme/base-c\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        repo_batch_size=1,
        rate_limit_floor=500,
    )
    refresh(SourceDefinition(name="prod", repos=["acme/a", "acme/b"]), source_key="source:prod")

    probe.rate_limit_remaining = 200
    partial = refresh(
        SourceDefinition(name="prod", repos=["acme/a", "acme/c"]),
        source_key="source:prod",
    )

    assert partial.completed is False
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")])
    assert index.dependents("acme/base-b", None, source_key="source:prod")

    probe.rate_limit_remaining = 1200
    completed = refresh(
        SourceDefinition(name="prod", repos=["acme/a", "acme/c"]),
        source_key="source:prod",
    )

    assert completed.completed is True
    assert index.ref_scans("source:prod", "acme/b", [("heads", "main")]) == {}
    assert not index.dependents("acme/base-b", None, source_key="source:prod")


def test_git_refresh_processes_repositories_concurrently_and_reports_change_counts(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = SlowGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = "- src: https://github.com/acme/base-a\n"
    git.files[("b", "sha-b", "roles/requirements.yml")] = "- src: https://github.com/acme/base-b\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        concurrency=2,
    )
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b", "acme/a"])

    first = refresh(source, source_key="source:prod")
    git.reads.clear()
    second = refresh(source, source_key="source:prod")

    assert first.changed_refs == 2
    assert first.unchanged_refs == 0
    assert second.changed_refs == 0
    assert second.unchanged_refs == 2
    assert second.edges == 2
    assert git.max_active_fetches > 1
    assert github.repository_calls.count(("acme", "a")) == 2
    assert github.repository_calls.count(("acme", "b")) == 2
    assert {fetch[0] for fetch in git.fetches} == {"a", "b"}
    assert [fetch[0] for fetch in git.fetches].count("a") == 1
    assert [fetch[0] for fetch in git.fetches].count("b") == 1
    assert git.reads == []


def test_git_refresh_reads_ref_metadata_in_one_batch_per_repo(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="heads", name="release", sha="sha-release"),
    ]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n"
    )
    git.files[("site", "sha-release", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/release-base\n"
    )
    index = CountingRefScanIndex(SqliteDependencyIndex(tmp_path / "index.sqlite3"))

    _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        ref_scan_default="default_branch",
    )(
        SourceDefinition(name="prod", orgs=["acme"], ref_patterns=["*"]),
        source_key="source:prod",
    )

    assert index.ref_scans_calls == [
        (
            "source:prod",
            "acme/site",
            (("heads", "main"), ("heads", "release")),
        )
    ]


def test_git_refresh_reads_sqlite_metadata_safely_while_fetching_concurrently(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = SlowGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/a"] = [GitRef(kind="heads", name="main", sha="sha-a")]
    probe.refs["acme/b"] = [GitRef(kind="heads", name="main", sha="sha-b")]
    index = SlowRefScanIndex(SqliteDependencyIndex(tmp_path / "index.sqlite3"))
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        concurrency=2,
    )

    refresh(SourceDefinition(name="prod", repos=["acme/a", "acme/b"]), source_key="source:prod")

    assert git.max_active_fetches > 1
    assert index.max_active_ref_scans > 1


def test_repo_candidate_does_not_guess_main_when_default_branch_is_missing() -> None:
    candidate = _repo_candidate({"full_name": "acme/site"}, fallback=None)

    assert candidate.default_branch == "HEAD"


def test_git_refresh_reuses_unchanged_ref_metadata_without_rereading_files(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)

    refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/changed\n"
    )
    second = refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert second.edges == 1
    assert git.reads == [("site", "sha-main", "roles/requirements.yml", None)]
    assert index.dependents("acme/base", "v1", source_key="source:prod")
    assert not index.dependents("acme/changed", None, source_key="source:prod")


def test_git_refresh_skips_bare_cache_work_for_unchanged_remote_refs(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)

    refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")
    git.ensure_calls.clear()
    git.fetches.clear()
    git.reads.clear()
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/changed\n"
    )
    second = refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert second.changed_refs == 0
    assert second.unchanged_refs == 1
    assert git.ensure_calls == []
    assert git.fetches == []
    assert git.reads == []
    assert index.dependents("acme/base", "v1", source_key="source:prod")
    assert not index.dependents("acme/changed", None, source_key="source:prod")


def test_git_refresh_fetches_only_changed_refs_and_prunes_deleted_remote_refs(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="heads", name="release", sha="sha-release"),
    ]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    git.files[("site", "sha-release", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/release-base\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        ref_scan_default="default_branch",
    )
    source = SourceDefinition(name="prod", orgs=["acme"], ref_patterns=["*"])

    refresh(source, source_key="source:prod")
    git.fetches.clear()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main-2")]
    git.files[("site", "sha-main-2", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v2\n"
    )
    refresh(source, source_key="source:prod")

    assert git.fetches == [("site", ("+refs/heads/main:refs/heads/main",), 1, True, None)]
    assert index.dependents("acme/base", "v2", source_key="source:prod")
    assert not index.dependents("acme/base", "v1", source_key="source:prod")
    assert not index.dependents("acme/release-base", None, source_key="source:prod")
    assert index.ref_scans("source:prod", "acme/site", [("heads", "release")]) == {}


def test_git_refresh_reuses_parsed_dependencies_for_duplicate_remote_shas(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="heads", name="main", sha="sha-shared"),
        GitRef(kind="heads", name="release", sha="sha-shared"),
    ]
    git.files[("site", "sha-shared", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        ref_scan_default="default_branch",
    )(
        SourceDefinition(name="prod", orgs=["acme"], ref_patterns=["*"]),
        source_key="source:prod",
    )

    assert git.reads == [("site", "sha-shared", "roles/requirements.yml", None)]
    assert {
        edge.source_ref for edge in index.dependents("acme/base", None, source_key="source:prod")
    } == {"main", "release"}


def test_git_refresh_reindexes_unchanged_ref_when_aliases_change(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = "- common\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    _make_refresh(github=github, git=git, probe=probe, index=index, tmp_path=tmp_path)(
        SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod"
    )
    _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        aliases={"common": "acme/common"},
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert git.reads == [
        ("site", "sha-main", "roles/requirements.yml", None),
        ("site", "sha-main", "roles/requirements.yml", None),
    ]
    assert index.dependents("acme/common", None, source_key="source:prod")
    assert not index.dependencies("acme/site", "main", source_key="source:prod")[0].unresolved


def test_git_refresh_reindexes_moved_tags_and_prunes_unselected_refs(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    probe = FakeRefProbe()
    probe.refs["acme/site"] = [
        GitRef(kind="tags", name="v1", sha="sha-v1"),
        GitRef(kind="tags", name="v-old", sha="sha-old"),
    ]
    git.files[("site", "sha-v1", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n"
    )
    git.files[("site", "sha-old", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/old\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = _make_refresh(
        github=github,
        git=git,
        probe=probe,
        index=index,
        tmp_path=tmp_path,
        clone_protocol="ssh",
        fetch_depth=0,
        blob_filter=False,
    )
    source = SourceDefinition(
        name="prod",
        orgs=["acme"],
        ref_kinds=["tags"],
        ref_patterns=["v*"],
    )

    refresh(source, source_key="source:prod")
    probe.refs["acme/site"] = [GitRef(kind="tags", name="v1", sha="sha-v2")]
    git.files[("site", "sha-v2", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v2\n"
    )
    refresh(source, source_key="source:prod")

    assert git.fetches[0] == (
        "site",
        ("+refs/tags/v-old:refs/tags/v-old", "+refs/tags/v1:refs/tags/v1"),
        0,
        False,
        None,
    )
    assert index.dependents("acme/base", "v2", source_key="source:prod")
    assert not index.dependents("acme/base", "v1", source_key="source:prod")
    assert not index.dependents("acme/old", None, source_key="source:prod")
    assert index.ref_scans("source:prod", "acme/site", [("tags", "v-old")]) == {}


def test_git_refresh_rejects_unknown_clone_protocol(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="clone_protocol"):
        _make_refresh(
            github=FakeGitHub(),
            git=FakeGitCache(),
            probe=FakeRefProbe(),
            index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
            tmp_path=tmp_path,
            clone_protocol="git",
        )


def test_git_refresh_rejects_invalid_refresh_batch_options(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repo_batch_size"):
        _make_refresh(
            github=FakeGitHub(),
            git=FakeGitCache(),
            probe=FakeRefProbe(),
            index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
            tmp_path=tmp_path,
            repo_batch_size=0,
        )
    with pytest.raises(ValueError, match="rate_limit_floor"):
        _make_refresh(
            github=FakeGitHub(),
            git=FakeGitCache(),
            probe=FakeRefProbe(),
            index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
            tmp_path=tmp_path,
            rate_limit_floor=-1,
        )
