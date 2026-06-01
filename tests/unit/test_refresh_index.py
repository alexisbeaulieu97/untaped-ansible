"""Tests for refreshing dependency index sources from GitHub."""

from __future__ import annotations

from untaped_ansible.application.refresh_index import RefreshSourceIndex
from untaped_ansible.infrastructure import IndexScan
from untaped_ansible.settings import SourceDefinition


class FakeGitHub:
    def list_org_repos(self, org: str) -> list[dict[str, object]]:
        assert org == "acme"
        return [{"full_name": "acme/site"}]

    def list_team_repos(self, org: str, team_slug: str) -> list[dict[str, object]]:
        assert (org, team_slug) == ("acme", "platform")
        return [{"full_name": "acme/platform-playbook"}]

    def list_matching_refs(self, owner: str, repo: str, namespace: str) -> list[dict[str, object]]:
        assert namespace in {"heads", "tags"}
        return [
            {
                "ref": f"refs/{namespace}/main",
                "object": {"sha": f"{owner}-{repo}-{namespace}-sha", "type": "commit"},
            },
            {
                "ref": f"refs/{namespace}/scratch",
                "object": {"sha": "scratch-sha", "type": "commit"},
            },
        ]

    def get_tree(
        self,
        owner: str,
        repo: str,
        tree_sha: str,
        *,
        recursive: bool = False,
    ) -> dict[str, object]:
        assert recursive
        return {"tree": [{"path": "roles/requirements.yml", "type": "blob"}]}

    def get_raw_content(self, owner: str, repo: str, path: str, *, ref: str) -> str:
        assert path == "roles/requirements.yml"
        return """
        roles:
          - src: https://github.com/acme/base
            version: v1
          - common
        collections:
          - name: community.general
        """


class CapturingIndex:
    def __init__(self) -> None:
        self.scan: IndexScan | None = None

    def replace_source_scan(self, scan: IndexScan) -> None:
        self.scan = scan


def test_refresh_index_expands_source_refs_parses_dependencies_and_aliases() -> None:
    index = CapturingIndex()
    source = SourceDefinition(
        name="prod",
        orgs=["acme"],
        teams=["acme/platform"],
        repos=["acme/explicit"],
        ref_kinds=["heads"],
        ref_patterns=["main"],
    )

    result = RefreshSourceIndex(
        github=FakeGitHub(),
        index=index,
        aliases={"common": "acme/common"},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.repos == 3
    assert result.refs == 3
    assert result.edges == 6
    assert index.scan is not None
    assert {edge.source_repo for edge in index.scan.dependencies} == {
        "acme/site",
        "acme/platform-playbook",
        "acme/explicit",
    }
    assert {edge.dependency_repo for edge in index.scan.dependencies} == {
        "acme/base",
        "acme/common",
    }
    assert {edge.source_sha for edge in index.scan.dependencies} == {
        "acme-explicit-heads-sha",
        "acme-platform-playbook-heads-sha",
        "acme-site-heads-sha",
    }
    assert result.ignored_collections == ("community.general",)


def test_refresh_index_expands_bare_team_slug_with_single_source_org() -> None:
    index = CapturingIndex()
    source = SourceDefinition(
        name="prod",
        orgs=["acme"],
        teams=["platform"],
        ref_kinds=["heads"],
        ref_patterns=["main"],
    )

    result = RefreshSourceIndex(
        github=FakeGitHub(),
        index=index,
        aliases={"common": "acme/common"},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.repos == 2
    assert index.scan is not None
    assert {edge.source_repo for edge in index.scan.dependencies} == {
        "acme/site",
        "acme/platform-playbook",
    }
