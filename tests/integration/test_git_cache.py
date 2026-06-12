"""Integration tests for the bare Git dependency cache."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from untaped_ansible.infrastructure.git_cache import GitRepositoryCache

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    full_path = repo / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_bare_git_cache_fetches_branch_updates_and_reads_files_without_checkout(
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    _git(tmp_path, "init", str(upstream))
    _git(upstream, "config", "user.email", "tests@example.com")
    _git(upstream, "config", "user.name", "Tests")
    _git(upstream, "config", "commit.gpgsign", "false")
    first_sha = _commit(
        upstream,
        "roles/requirements.yml",
        "- src: https://github.com/acme/base\n  version: v1\n",
        "first",
    )
    _git(upstream, "branch", "-M", "main")

    cache = GitRepositoryCache()
    bare = cache.ensure_bare(f"file://{upstream}", cache_dir=tmp_path / "cache", auth_header=None)
    cache.fetch_refs(
        bare,
        refspecs=["+refs/heads/main:refs/heads/main"],
        depth=1,
        blob_filter=False,
        auth_header=None,
    )

    assert _git(bare, "rev-parse", "refs/heads/main") == first_sha
    assert cache.read_file(bare, first_sha, "roles/requirements.yml", auth_header=None) is not None
    assert not (bare / "roles").exists()

    second_sha = _commit(
        upstream,
        "roles/requirements.yml",
        "- src: https://github.com/acme/base\n  version: v2\n",
        "second",
    )
    cache.fetch_refs(
        bare,
        refspecs=["+refs/heads/main:refs/heads/main"],
        depth=1,
        blob_filter=False,
        auth_header=None,
    )

    assert _git(bare, "rev-parse", "refs/heads/main") == second_sha
    assert "version: v2" in (
        cache.read_file(bare, second_sha, "roles/requirements.yml", auth_header=None) or ""
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_bare_git_cache_peels_annotated_tags_for_file_reads(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _git(tmp_path, "init", str(upstream))
    _git(upstream, "config", "user.email", "tests@example.com")
    _git(upstream, "config", "user.name", "Tests")
    _git(upstream, "config", "commit.gpgsign", "false")
    commit_sha = _commit(
        upstream,
        "roles/requirements.yml",
        "- src: https://github.com/acme/base\n  version: v1\n",
        "first",
    )
    _git(upstream, "tag", "-a", "v1", "-m", "v1")
    tag_object_sha = _git(upstream, "rev-parse", "refs/tags/v1")
    peeled_sha = _git(upstream, "rev-parse", "refs/tags/v1^{commit}")

    cache = GitRepositoryCache()
    bare = cache.ensure_bare(f"file://{upstream}", cache_dir=tmp_path / "cache", auth_header=None)
    cache.fetch_refs(
        bare,
        refspecs=["+refs/tags/*:refs/tags/*"],
        depth=1,
        blob_filter=False,
        auth_header=None,
    )

    # The freshness probe reports the peeled commit sha for annotated tags;
    # file reads against that sha must work from the fetched tag object.
    assert peeled_sha == commit_sha
    assert peeled_sha != tag_object_sha
    assert "version: v1" in (
        cache.read_file(bare, peeled_sha, "roles/requirements.yml", auth_header=None) or ""
    )
