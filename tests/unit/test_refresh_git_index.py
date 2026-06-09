"""Tests for refreshing dependency sources through a local Git cache."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from untaped_ansible.application.refresh_git_index import (
    RefreshGitSourceIndex,
    _RefSelection,
    _repo_candidate,
    _selected_refs,
)
from untaped_ansible.domain.payloads import CachedRef, GitRef
from untaped_ansible.infrastructure.git_cache import GitCacheError
from untaped_ansible.infrastructure.sqlite_index import SqliteDependencyIndex
from untaped_ansible.settings import SourceDefinition


class FakeGitHub:
    def __init__(self) -> None:
        self.repository_calls: list[tuple[str, str]] = []
        self.org_calls: list[str] = []

    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        self.repository_calls.append((owner, repo))
        return {
            "full_name": f"{owner}/{repo}",
            "default_branch": "main",
            "clone_url": f"https://github.com/{owner}/{repo}.git",
            "ssh_url": f"git@github.com:{owner}/{repo}.git",
        }

    def list_org_repos(self, org: str) -> list[dict[str, object]]:
        self.org_calls.append(org)
        return [
            {
                "full_name": f"{org}/site",
                "default_branch": "main",
                "clone_url": f"https://github.com/{org}/site.git",
                "ssh_url": f"git@github.com:{org}/site.git",
            }
        ]

    def list_team_repos(self, org: str, team_slug: str) -> list[dict[str, object]]:
        return []

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


class FakeGitCache:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], list[GitRef]] = {}
        self.remote_refs: dict[str, list[GitRef]] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        self.ensure_calls: list[tuple[str, Path, str | None]] = []
        self.fetches: list[tuple[str, tuple[str, ...], int, bool, str | None]] = []
        self.reads: list[tuple[str, str, str, str | None]] = []
        self.list_ref_calls: list[tuple[str, str]] = []
        self.list_remote_ref_calls: list[tuple[str, tuple[str, ...], str | None]] = []
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

    def list_remote_refs(
        self,
        url: str,
        *,
        namespaces: list[str],
        auth_header: str | None,
    ) -> list[GitRef]:
        self.list_remote_ref_calls.append((url, tuple(namespaces), auth_header))
        if url in self.remote_refs:
            refs = self.remote_refs[url]
        else:
            repo_name = url.removesuffix(".git").rsplit("/", maxsplit=1)[-1]
            refs = [
                ref
                for (bare_name, _kind), values in self.refs.items()
                if bare_name == repo_name
                for ref in values
            ]
        return [
            ref
            for ref in refs
            if any(_fake_namespace_matches(ref, namespace) for namespace in namespaces)
        ]

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

    def list_refs(self, bare_path: Path, kind: str) -> list[GitRef]:
        self.list_ref_calls.append((bare_path.name, kind))
        return self.refs.get((bare_path.name, kind), [])

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


def _fake_namespace_matches(ref: GitRef, namespace: str) -> bool:
    kind, _, pattern = namespace.partition("/")
    if kind != ref.kind:
        return False
    if not pattern:
        return True
    from fnmatch import fnmatch

    return fnmatch(ref.name, pattern)


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


def test_git_refresh_fetches_selected_refs_and_indexes_dependency_files(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    git.refs[("site", "heads")] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header="AUTHORIZATION: bearer test",
        ref_scan_default="default_branch",
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert result.repos == 1
    assert result.refs == 1
    assert result.edges == 1
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
    git.refs[("site", "heads")] = [GitRef(kind="heads", name="master", sha="sha-master")]
    git.refs[("site", "tags")] = [GitRef(kind="tags", name="v3", sha="sha-v3")]
    git.files[("site", "sha-master", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v3\n"
    )
    git.files[("site", "sha-v3", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v3\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    result = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
        ref_scan_default="all",
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert result.refs == 2
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


def test_git_refresh_processes_repositories_concurrently_and_reports_change_counts(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = SlowGitCache()
    git.refs[("a", "heads")] = [GitRef(kind="heads", name="main", sha="sha-a")]
    git.refs[("b", "heads")] = [GitRef(kind="heads", name="main", sha="sha-b")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = "- src: https://github.com/acme/base-a\n"
    git.files[("b", "sha-b", "roles/requirements.yml")] = "- src: https://github.com/acme/base-b\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
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
    git.refs[("site", "heads")] = [
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

    RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
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
    git.refs[("a", "heads")] = [GitRef(kind="heads", name="main", sha="sha-a")]
    git.refs[("b", "heads")] = [GitRef(kind="heads", name="main", sha="sha-b")]
    index = SlowRefScanIndex(SqliteDependencyIndex(tmp_path / "index.sqlite3"))
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
        concurrency=2,
    )

    refresh(SourceDefinition(name="prod", repos=["acme/a", "acme/b"]), source_key="source:prod")

    assert git.max_active_fetches > 1
    assert index.max_active_ref_scans > 1


def test_selected_refs_lists_each_ref_kind_once() -> None:
    git = FakeGitCache()
    bare = Path("site")
    git.refs[("site", "heads")] = [
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="heads", name="release/1", sha="sha-release"),
    ]

    refs = _selected_refs(
        git,
        bare,
        [
            _RefSelection(
                kind="heads",
                patterns=("main",),
                refspecs=("+refs/heads/main:refs/heads/main",),
            ),
            _RefSelection(
                kind="heads",
                patterns=("release/*",),
                refspecs=("+refs/heads/release/*:refs/heads/release/*",),
            ),
        ],
    )

    assert [(ref.kind, ref.name) for ref in refs] == [
        ("heads", "main"),
        ("heads", "release/1"),
    ]
    assert git.list_ref_calls == [("site", "heads")]


def test_repo_candidate_does_not_guess_main_when_default_branch_is_missing() -> None:
    candidate = _repo_candidate({"full_name": "acme/site"}, fallback=None)

    assert candidate.default_branch == "HEAD"


def test_git_refresh_reuses_unchanged_ref_metadata_without_rereading_files(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    git.refs[("site", "heads")] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
    )

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
    git.refs[("site", "heads")] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
    )

    refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")
    git.ensure_calls.clear()
    git.fetches.clear()
    git.list_ref_calls.clear()
    git.reads.clear()
    git.files[("site", "sha-main", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/changed\n"
    )
    second = refresh(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert second.changed_refs == 0
    assert second.unchanged_refs == 1
    assert git.ensure_calls == []
    assert git.fetches == []
    assert git.list_ref_calls == []
    assert git.reads == []
    assert index.dependents("acme/base", "v1", source_key="source:prod")
    assert not index.dependents("acme/changed", None, source_key="source:prod")


def test_git_refresh_fetches_only_changed_refs_and_prunes_deleted_remote_refs(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    git.refs[("site", "heads")] = [
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
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
        ref_scan_default="default_branch",
    )
    source = SourceDefinition(name="prod", orgs=["acme"], ref_patterns=["*"])

    refresh(source, source_key="source:prod")
    git.fetches.clear()
    git.refs[("site", "heads")] = [GitRef(kind="heads", name="main", sha="sha-main-2")]
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
    git.refs[("site", "heads")] = [
        GitRef(kind="heads", name="main", sha="sha-shared"),
        GitRef(kind="heads", name="release", sha="sha-shared"),
    ]
    git.files[("site", "sha-shared", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
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
    git.refs[("site", "heads")] = [GitRef(kind="heads", name="main", sha="sha-main")]
    git.files[("site", "sha-main", "roles/requirements.yml")] = "- common\n"
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")

    RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")
    RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={"common": "acme/common"},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
    )(SourceDefinition(name="prod", orgs=["acme"]), source_key="source:prod")

    assert git.reads == [
        ("site", "sha-main", "roles/requirements.yml", None),
        ("site", "sha-main", "roles/requirements.yml", None),
    ]
    assert index.dependents("acme/common", None, source_key="source:prod")
    assert not index.dependencies("acme/site", "main", source_key="source:prod")[0].unresolved


def test_failed_git_refresh_does_not_advance_source_status(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    git.refs[("a", "heads")] = [GitRef(kind="heads", name="main", sha="sha-a")]
    git.refs[("b", "heads")] = [GitRef(kind="heads", name="main", sha="sha-b")]
    git.files[("a", "sha-a", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    git.files[("b", "sha-b", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v1\n"
    )
    index = SqliteDependencyIndex(tmp_path / "index.sqlite3")
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="https",
        fetch_depth=1,
        blob_filter=True,
        auth_header=None,
    )
    source = SourceDefinition(name="prod", repos=["acme/a", "acme/b"])

    refresh(source, source_key="source:prod")
    previous = index.status("source:prod")
    assert previous is not None
    git.refs[("a", "heads")] = [GitRef(kind="heads", name="main", sha="sha-a2")]
    git.refs[("b", "heads")] = [GitRef(kind="heads", name="main", sha="sha-b2")]
    git.files[("a", "sha-a2", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v2\n"
    )
    git.files[("b", "sha-b2", "roles/requirements.yml")] = (
        "- src: https://github.com/acme/base\n  version: v2\n"
    )
    git.fail_fetches.add("b")

    with pytest.raises(GitCacheError, match="git fetch failed for b"):
        refresh(source, source_key="source:prod")

    current = index.status("source:prod")
    assert current is not None
    assert current.scanned_at == previous.scanned_at
    assert not index.dependents("acme/base", "v2", source_key="source:prod")
    v1_source_repos = {
        edge.source_repo for edge in index.dependents("acme/base", "v1", source_key="source:prod")
    }
    assert v1_source_repos == {
        "acme/a",
        "acme/b",
    }


def test_git_refresh_reindexes_moved_tags_and_prunes_unselected_refs(tmp_path: Path) -> None:
    github = FakeGitHub()
    git = FakeGitCache()
    git.refs[("site", "tags")] = [
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
    refresh = RefreshGitSourceIndex(
        github=github,
        git=git,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        repo_cache_path=tmp_path / "repos",
        clone_protocol="ssh",
        fetch_depth=0,
        blob_filter=False,
        auth_header=None,
    )
    source = SourceDefinition(
        name="prod",
        orgs=["acme"],
        ref_kinds=["tags"],
        ref_patterns=["v*"],
    )

    refresh(source, source_key="source:prod")
    git.refs[("site", "tags")] = [GitRef(kind="tags", name="v1", sha="sha-v2")]
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
        RefreshGitSourceIndex(
            github=FakeGitHub(),
            git=FakeGitCache(),
            index=SqliteDependencyIndex(tmp_path / "index.sqlite3"),
            aliases={},
            default_dependency_paths=["roles/requirements.yml"],
            repo_cache_path=tmp_path / "repos",
            clone_protocol="git",
            fetch_depth=1,
            blob_filter=True,
            auth_header=None,
        )
