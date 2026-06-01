"""Tests for refreshing dependency index sources from GitHub."""

from __future__ import annotations

from untaped_ansible.application.refresh_index import RefreshSourceIndex
from untaped_ansible.infrastructure import IndexScan
from untaped_ansible.settings import SourceDefinition


class FakeGitHub:
    def __init__(self) -> None:
        self.ref_calls: list[tuple[str, str, str]] = []
        self.repository_calls: list[tuple[str, str]] = []

    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        self.repository_calls.append((owner, repo))
        return {"default_branch": "main"}

    def list_org_repos(self, org: str) -> list[dict[str, object]]:
        assert org == "acme"
        return [{"full_name": "acme/site"}]

    def list_team_repos(self, org: str, team_slug: str) -> list[dict[str, object]]:
        assert (org, team_slug) == ("acme", "platform")
        return [{"full_name": "acme/platform-playbook"}]

    def list_matching_refs(self, owner: str, repo: str, namespace: str) -> list[dict[str, object]]:
        self.ref_calls.append((owner, repo, namespace))
        kind, _, pattern = namespace.partition("/")
        assert kind in {"heads", "tags"}
        candidates = ["main", "scratch"]
        if pattern:
            candidates = [candidate for candidate in candidates if candidate.startswith(pattern)]
        return [
            {
                "ref": f"refs/{kind}/{candidate}",
                "object": {"sha": f"{owner}-{repo}-{kind}-sha", "type": "commit"},
            }
            for candidate in candidates
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


def test_refresh_index_can_default_to_default_branch_for_each_source_repo() -> None:
    index = CapturingIndex()
    github = FakeGitHub()
    source = SourceDefinition(
        name="prod",
        orgs=["acme"],
        teams=["acme/platform"],
        repos=["acme/explicit"],
    )

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={"common": "acme/common"},
        default_dependency_paths=["roles/requirements.yml"],
        ref_scan_default="default_branch",
    )(source, source_key="source:prod")

    assert result.refs == 3
    assert github.repository_calls == [
        ("acme", "explicit"),
        ("acme", "platform-playbook"),
        ("acme", "site"),
    ]
    assert github.ref_calls == [
        ("acme", "explicit", "heads/main"),
        ("acme", "platform-playbook", "heads/main"),
        ("acme", "site", "heads/main"),
    ]


class PatternGitHub:
    def __init__(self, refs: dict[str, list[str]]) -> None:
        self.refs = refs
        self.ref_calls: list[str] = []

    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        return {"default_branch": "main"}

    def list_org_repos(self, org: str) -> list[dict[str, object]]:
        return []

    def list_team_repos(self, org: str, team_slug: str) -> list[dict[str, object]]:
        return []

    def list_matching_refs(self, owner: str, repo: str, namespace: str) -> list[dict[str, object]]:
        self.ref_calls.append(namespace)
        kind, _, _pattern = namespace.partition("/")
        return [
            {"ref": f"refs/{kind}/{name}", "object": {"sha": f"sha-{name}"}}
            for name in self.refs.get(namespace, [])
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
        return "- src: https://github.com/acme/base\n"


def test_refresh_index_narrows_exact_and_slash_prefix_pattern_ref_calls() -> None:
    index = CapturingIndex()
    github = PatternGitHub(
        {
            "heads/main": ["main", "main-backup"],
            "heads/release/": ["release/2026.01"],
        }
    )
    source = SourceDefinition(
        name="prod",
        repos=["acme/site"],
        ref_kinds=["heads"],
        ref_patterns=["main", "release/*"],
    )

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.refs == 2
    assert github.ref_calls == ["heads/main", "heads/release/"]
    assert index.scan is not None
    assert [edge.source_ref for edge in index.scan.dependencies] == [
        "main",
        "release/2026.01",
    ]


def test_refresh_index_uses_whole_ref_kind_for_ambiguous_wildcard_prefix() -> None:
    index = CapturingIndex()
    github = PatternGitHub({"heads": ["main", "v", "v1", "v2"]})
    source = SourceDefinition(
        name="prod",
        repos=["acme/site"],
        ref_kinds=["heads"],
        ref_patterns=["v*"],
    )

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.refs == 3
    assert github.ref_calls == ["heads"]
    assert index.scan is not None
    assert [edge.source_ref for edge in index.scan.dependencies] == ["v", "v1", "v2"]


def test_refresh_index_wildcard_pattern_scans_whole_selected_ref_kind_once() -> None:
    index = CapturingIndex()
    github = PatternGitHub({"heads": ["main", "scratch"]})
    source = SourceDefinition(
        name="prod",
        repos=["acme/site"],
        ref_kinds=["heads"],
        ref_patterns=["*"],
    )

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.refs == 2
    assert github.ref_calls == ["heads"]


def test_refresh_index_defaults_to_all_heads_and_tags() -> None:
    index = CapturingIndex()
    github = PatternGitHub(
        {
            "heads": ["master"],
            "tags": ["v3"],
        }
    )
    source = SourceDefinition(name="prod", repos=["acme/site"])

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
        ref_scan_default="all",
    )(source, source_key="source:prod")

    assert result.refs == 2
    assert github.ref_calls == ["heads", "tags"]
    assert index.scan is not None
    assert [edge.source_ref for edge in index.scan.dependencies] == ["master", "v3"]


def test_refresh_index_wildcard_pattern_dedupes_narrower_ref_calls() -> None:
    index = CapturingIndex()
    github = PatternGitHub({"heads": ["main", "scratch"]})
    source = SourceDefinition(
        name="prod",
        repos=["acme/site"],
        ref_kinds=["heads"],
        ref_patterns=["*", "main"],
    )

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.refs == 2
    assert github.ref_calls == ["heads"]


def test_refresh_index_keeps_explicit_tag_scans_available() -> None:
    index = CapturingIndex()
    github = PatternGitHub({"tags": ["main", "v1", "v2"]})
    source = SourceDefinition(
        name="prod",
        repos=["acme/site"],
        ref_kinds=["tags"],
        ref_patterns=["v*"],
    )

    result = RefreshSourceIndex(
        github=github,
        index=index,
        aliases={},
        default_dependency_paths=["roles/requirements.yml"],
    )(source, source_key="source:prod")

    assert result.refs == 2
    assert github.ref_calls == ["tags"]
    assert index.scan is not None
    assert [edge.source_ref for edge in index.scan.dependencies] == ["v1", "v2"]
