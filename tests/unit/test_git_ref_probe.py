"""Tests for the git ls-remote ref freshness probe adapter."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from untaped_ansible.domain.payloads import GitRef, ProbeTarget
from untaped_ansible.infrastructure.git_cache import GitCacheError, GitRepositoryCache
from untaped_ansible.infrastructure.git_ref_probe import GitRemoteRefProbe


class FakeGit:
    def __init__(self) -> None:
        self.outputs: dict[str, str] = {}
        self.failures: dict[str, Exception] = {}
        self.calls: list[tuple[str, tuple[str, ...], str | None]] = []

    def ls_remote(
        self,
        url: str,
        *,
        patterns: list[str],
        auth_header: str | None,
    ) -> str:
        self.calls.append((url, tuple(patterns), auth_header))
        failure = self.failures.get(url)
        if failure is not None:
            raise failure
        return self.outputs.get(url, "")


def test_git_probe_all_mode_parses_branches_tags_and_peeled_tags() -> None:
    git = FakeGit()
    git.outputs["https://github.com/acme/site.git"] = "\n".join(
        [
            "ref: refs/heads/main\tHEAD",
            "sha-head\tHEAD",
            "sha-dev\trefs/heads/dev",
            "sha-main\trefs/heads/main",
            "sha-light\trefs/tags/v1",
            "sha-tag-object\trefs/tags/v2",
            "sha-peeled\trefs/tags/v2^{}",
            "",
        ]
    )
    target = ProbeTarget(
        full_name="acme/site",
        default_branch="main",
        clone_url="https://github.com/acme/site.git",
    )

    report = GitRemoteRefProbe(git, clone_protocol="https", auth_header="AUTH").probe(
        [target],
        kinds=("heads", "tags"),
    )

    assert report.failures == {}
    assert report.repos["acme/site"].default_branch == "main"
    assert report.repos["acme/site"].refs == (
        GitRef(kind="heads", name="dev", sha="sha-dev"),
        GitRef(kind="heads", name="main", sha="sha-main"),
        GitRef(kind="tags", name="v1", sha="sha-light"),
        GitRef(kind="tags", name="v2", sha="sha-peeled"),
    )
    assert git.calls == [
        (
            "https://github.com/acme/site.git",
            ("HEAD", "refs/heads/*", "refs/tags/*"),
            "AUTH",
        )
    ]


def test_git_probe_respects_requested_ref_kinds() -> None:
    git = FakeGit()
    git.outputs["https://github.com/acme/site.git"] = "sha-main\trefs/heads/main\n"
    target = ProbeTarget(
        full_name="acme/site",
        default_branch="main",
        clone_url="https://github.com/acme/site.git",
    )

    GitRemoteRefProbe(git, clone_protocol="https", auth_header=None).probe(
        [target],
        kinds=("heads",),
    )

    assert git.calls == [("https://github.com/acme/site.git", ("HEAD", "refs/heads/*"), None)]


def test_git_probe_default_branch_mode_resolves_head_symref() -> None:
    git = FakeGit()
    git.outputs["https://github.com/acme/site.git"] = "\n".join(
        [
            "ref: refs/heads/trunk\tHEAD",
            "sha-trunk\tHEAD",
            "sha-trunk\trefs/heads/trunk",
            "",
        ]
    )
    target = ProbeTarget(
        full_name="acme/site",
        default_branch="main",
        clone_url="https://github.com/acme/site.git",
    )

    report = GitRemoteRefProbe(git, clone_protocol="https", auth_header=None).probe(
        [target],
        kinds=("heads", "tags"),
        mode="default_branch",
    )

    assert report.repos["acme/site"].default_branch == "trunk"
    assert report.repos["acme/site"].refs == (GitRef(kind="heads", name="trunk", sha="sha-trunk"),)
    assert git.calls == [("https://github.com/acme/site.git", ("HEAD", "refs/heads/main"), None)]


def test_git_probe_default_branch_mode_falls_back_to_inventory_branch() -> None:
    git = FakeGit()
    git.outputs["https://github.com/acme/site.git"] = "sha-main\trefs/heads/main\n"
    target = ProbeTarget(
        full_name="acme/site",
        default_branch="main",
        clone_url="https://github.com/acme/site.git",
    )

    report = GitRemoteRefProbe(git, clone_protocol="https", auth_header=None).probe(
        [target],
        kinds=("heads", "tags"),
        mode="default_branch",
    )

    assert report.repos["acme/site"].default_branch == "main"
    assert report.repos["acme/site"].refs == (GitRef(kind="heads", name="main", sha="sha-main"),)


def test_git_probe_reports_git_failures_per_repo() -> None:
    git = FakeGit()
    git.failures["https://github.com/acme/site.git"] = GitCacheError("git ls-remote failed")
    target = ProbeTarget(
        full_name="acme/site",
        default_branch="main",
        clone_url="https://github.com/acme/site.git",
    )

    report = GitRemoteRefProbe(git, clone_protocol="https", auth_header=None).probe(
        [target],
        kinds=("heads",),
    )

    assert report.repos == {}
    assert report.failures["acme/site"].kind == "git"
    assert report.failures["acme/site"].reason == "git ref probe failed: git ls-remote failed"


def test_git_probe_empty_output_is_success_with_no_refs() -> None:
    git = FakeGit()
    target = ProbeTarget(
        full_name="acme/site",
        default_branch="main",
        clone_url="https://github.com/acme/site.git",
    )

    report = GitRemoteRefProbe(git, clone_protocol="https", auth_header=None).probe(
        [target],
        kinds=("heads",),
    )

    assert report.repos["acme/site"].default_branch == "main"
    assert report.repos["acme/site"].refs == ()
    assert report.failures == {}


def test_git_probe_reports_progress() -> None:
    git = FakeGit()
    git.outputs["https://github.com/acme/a.git"] = ""
    git.outputs["https://github.com/acme/b.git"] = ""
    progress: list[tuple[int, int]] = []
    targets = [
        ProbeTarget(
            full_name="acme/a", default_branch="main", clone_url="https://github.com/acme/a.git"
        ),
        ProbeTarget(
            full_name="acme/b", default_branch="main", clone_url="https://github.com/acme/b.git"
        ),
    ]

    GitRemoteRefProbe(git, clone_protocol="https", auth_header=None, concurrency=1).probe(
        targets,
        kinds=("heads",),
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert progress == [(1, 2), (2, 2)]


def test_git_probe_validates_construction_arguments() -> None:
    git = FakeGit()

    with pytest.raises(ValueError, match="clone_protocol"):
        GitRemoteRefProbe(git, clone_protocol="file", auth_header=None)
    with pytest.raises(ValueError, match="concurrency"):
        GitRemoteRefProbe(git, clone_protocol="https", auth_header=None, concurrency=0)


def test_git_probe_uses_real_local_ls_remote_subprocess(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    bare = tmp_path / "remote.git"
    _git(["init", "-q", str(worktree)])
    _git(["config", "user.email", "test@example.com"], cwd=worktree)
    _git(["config", "user.name", "Tester"], cwd=worktree)
    _git(["config", "commit.gpgsign", "false"], cwd=worktree)
    _git(["config", "tag.gpgSign", "false"], cwd=worktree)
    (worktree / "README.md").write_text("hello\n")
    _git(["add", "README.md"], cwd=worktree)
    _git(["commit", "-q", "-m", "initial"], cwd=worktree)
    _git(["tag", "v-light"], cwd=worktree)
    _git(["tag", "-a", "v-ann", "-m", "annotated"], cwd=worktree)
    _git(["tag", "-a", "v-tag-of-tag", "v-ann", "-m", "tag of tag"], cwd=worktree)
    _git(["tag", "-a", "v-deep", "v-tag-of-tag", "-m", "deep tag"], cwd=worktree)
    _git(["clone", "--bare", "-q", str(worktree), str(bare)])

    commit_sha = _git(["rev-parse", "HEAD"], cwd=worktree).strip()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree).strip()
    target = ProbeTarget(full_name="acme/site", default_branch=branch, clone_url=str(bare))

    report = GitRemoteRefProbe(
        GitRepositoryCache(),
        clone_protocol="https",
        auth_header=None,
        concurrency=1,
    ).probe([target], kinds=("heads", "tags"))

    refs = {(ref.kind, ref.name): ref.sha for ref in report.repos["acme/site"].refs}
    assert refs[("heads", branch)] == commit_sha
    assert refs[("tags", "v-light")] == commit_sha
    assert refs[("tags", "v-ann")] == commit_sha
    assert refs[("tags", "v-tag-of-tag")] == commit_sha
    # git ls-remote exposes the tag object and the fully peeled target, but not
    # intermediate tag objects. Deeper tag chains therefore use Git's full peel.
    assert refs[("tags", "v-deep")] == commit_sha


def _git(args: Sequence[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout
