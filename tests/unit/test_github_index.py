"""Tests for live GitHub dependency reads."""

from __future__ import annotations

from untaped_ansible.domain.payloads import IndexedDependency
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

    def cached_refs(self, repo: str, *, source_key: str | None) -> set[str]:
        return set()

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
