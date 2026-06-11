"""Tests for live GitHub dependency reads."""

from __future__ import annotations

from collections.abc import Sequence

from untaped_ansible.domain.payloads import CachedRef, IndexedDependency
from untaped_ansible.infrastructure.github_index import GithubDependencyIndex


class StubGithub:
    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        assert (owner, repo) == ("acme", "site")
        return {"default_branch": "main"}

    def list_matching_refs(self, owner: str, repo: str, namespace: str) -> list[dict[str, object]]:
        raise AssertionError("no-ref dependency reads should use the default branch")

    def get_tree(
        self,
        owner: str,
        repo: str,
        tree_sha: str,
        *,
        recursive: bool = False,
    ) -> dict[str, object]:
        assert (owner, repo, tree_sha, recursive) == ("acme", "site", "main", True)
        return {"tree": [{"path": "roles/requirements.yml"}]}

    def get_raw_content(self, owner: str, repo: str, path: str, *, ref: str) -> str:
        assert (owner, repo, path, ref) == ("acme", "site", "roles/requirements.yml", "main")
        return "- src: https://github.com/acme/base\n"


class EmptyIndex:
    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return []

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return []

    def dependents_batch(
        self,
        pairs: Sequence[tuple[str, str | None]],
        *,
        source_key: str | None,
    ) -> dict[tuple[str, str | None], list[IndexedDependency]]:
        return {(repo, ref): [] for repo, ref in pairs}

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        return set()

    def cached_ref_metadata(self, repo: str, *, source_key: str | None) -> tuple[CachedRef, ...]:
        return ()

    def cached_ref_metadata_batch(
        self,
        repos: Sequence[str],
        *,
        source_key: str | None,
    ) -> dict[str, tuple[CachedRef, ...]]:
        return {repo: () for repo in repos}

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return False


def test_live_dependencies_without_ref_keep_default_branch_as_source_ref() -> None:
    index = GithubDependencyIndex(
        github=StubGithub(),
        wrapped=EmptyIndex(),
        aliases={},
        dependency_paths=["roles/requirements.yml"],
    )

    edges = index.dependencies("acme/site", None, source_key=None)

    assert [(edge.source_repo, edge.source_ref, edge.dependency_repo) for edge in edges] == [
        ("acme/site", "main", "acme/base")
    ]


def test_dependencies_batch_reads_live_per_pair_and_augments_cached_ref_reads() -> None:
    index = GithubDependencyIndex(
        github=StubGithub(),
        wrapped=EmptyIndex(),
        aliases={},
        dependency_paths=["roles/requirements.yml"],
    )

    batch = index.dependencies_batch([("acme/site", None)], source_key=None)

    assert [(edge.source_repo, edge.dependency_repo) for edge in batch[("acme/site", None)]] == [
        ("acme/site", "acme/base")
    ]
    # The live read for the default branch is cached per (repo, ref) pair, so a
    # repeated batch read returns the same edges without touching GitHub again.
    assert index.dependencies_batch([("acme/site", None)], source_key=None) == batch
    assert index.dependents_batch([("acme/base", None)], source_key=None) == {
        ("acme/base", None): []
    }
    assert index.cached_ref_metadata_batch(["acme/site"], source_key=None) == {"acme/site": ()}


def test_cached_ref_reads_include_live_fetched_refs() -> None:
    class RefStubGithub(StubGithub):
        def get_tree(
            self,
            owner: str,
            repo: str,
            tree_sha: str,
            *,
            recursive: bool = False,
        ) -> dict[str, object]:
            return {"tree": [{"path": "roles/requirements.yml"}]}

        def get_raw_content(self, owner: str, repo: str, path: str, *, ref: str) -> str:
            return "- src: https://github.com/acme/base\n"

        def list_matching_refs(
            self, owner: str, repo: str, namespace: str
        ) -> list[dict[str, object]]:
            return []

    index = GithubDependencyIndex(
        github=RefStubGithub(),
        wrapped=EmptyIndex(),
        aliases={},
        dependency_paths=["roles/requirements.yml"],
    )
    index.dependencies("acme/site", "release", source_key=None)

    assert index.cached_refs("acme/site", source_key=None) == {"release"}
    assert index.cached_ref_metadata("acme/site", source_key=None) == (CachedRef(name="release"),)
    assert index.cached_ref_metadata_batch(["acme/site", "acme/other"], source_key=None) == {
        "acme/site": (CachedRef(name="release"),),
        "acme/other": (),
    }
