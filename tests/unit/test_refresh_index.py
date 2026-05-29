"""Tests for refreshing dependency index scopes from GitHub."""

from __future__ import annotations

from untaped_ansible.application.refresh_index import RefreshIndex
from untaped_ansible.infrastructure import IndexScan
from untaped_ansible.settings import ScopeDefinition


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

    def replace_scope_scan(self, scan: IndexScan) -> None:
        self.scan = scan


def test_refresh_index_expands_scope_refs_parses_dependencies_and_aliases() -> None:
    index = CapturingIndex()
    scope = ScopeDefinition(
        name="prod",
        orgs=["acme"],
        teams=["acme/platform"],
        repos=["acme/explicit"],
        ref_kinds=["heads"],
        ref_patterns=["main"],
    )

    result = RefreshIndex(
        github=FakeGitHub(),
        index=index,
        aliases={"common": "acme/common"},
        default_dependency_paths=["roles/requirements.yml"],
    )(scope)

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
