"""Settings and state models contributed by the Ansible plugin."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

DEFAULT_DEPENDENCY_PATHS = (
    "roles/requirements.yml",
    "roles/requirements.yaml",
    "requirements.yml",
    "requirements.yaml",
    "meta/requirements.yml",
    "meta/requirements.yaml",
    "meta/main.yml",
)
ALLOWED_REF_KINDS = ("heads", "tags")
DEFAULT_REF_KINDS = ("heads",)


class SourceDefinition(BaseModel):
    """Named GitHub search boundary for index refresh and impact queries."""

    name: str
    orgs: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    dependency_paths: list[str] = Field(default_factory=list)
    ref_kinds: list[str] = Field(default_factory=lambda: list(DEFAULT_REF_KINDS))
    ref_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_source(self) -> Self:
        self.orgs = _dedupe_sorted(self.orgs)
        self.teams = normalize_team_refs(_dedupe_sorted(self.teams), self.orgs)
        self.repos = _dedupe_sorted(self.repos)
        self.dependency_paths = _dedupe_sorted(self.dependency_paths)
        self.ref_kinds = _dedupe_sorted(self.ref_kinds)
        self.ref_patterns = _dedupe_sorted(self.ref_patterns)
        if not any((self.orgs, self.teams, self.repos)):
            raise ValueError("source requires --org, --team, or --repo")
        for repo in self.repos:
            if not _is_repo_name(repo):
                raise ValueError(f"repo must be owner/name: {repo!r}")
        invalid_ref_kinds = sorted(set(self.ref_kinds) - set(ALLOWED_REF_KINDS))
        if invalid_ref_kinds:
            raise ValueError("ref-kind must be heads or tags")
        if "tags" in self.ref_kinds and not self.ref_patterns:
            raise ValueError("tag scans require --ref-pattern (use '*' for all tags)")
        return self


class AnsibleSettings(BaseModel):
    """User-tunable profile settings."""

    index_path: Path = Path("~/.untaped/ansible-index.sqlite3")
    stale_after: int = 86_400
    cache_backend: Literal["git", "api"] = "git"
    repo_cache_path: Path = Path("~/.untaped/ansible-repositories")
    git_clone_protocol: Literal["https", "ssh"] = "https"
    git_fetch_depth: int = Field(default=1, ge=0)
    git_fetch_concurrency: int = Field(default=8, ge=1, le=32)
    git_blob_filter: bool = True
    dependency_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_DEPENDENCY_PATHS))


class AnsibleState(BaseModel):
    """Top-level Ansible plugin app state."""

    sources: list[SourceDefinition] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)


def normalize_team_refs(teams: list[str], orgs: list[str]) -> list[str]:
    """Expand bare team slugs when the source has one unambiguous org."""
    normalized: list[str] = []
    for team in teams:
        if "/" in team:
            normalized.append(team)
            continue
        if len(orgs) == 1:
            normalized.append(f"{orgs[0]}/{team}")
            continue
        raise ValueError(f"team {team!r} must be ORG/SLUG unless the source has exactly one org")
    return normalized


def _dedupe_sorted(values: list[str]) -> list[str]:
    return sorted(dict.fromkeys(values))


def _is_repo_name(value: str) -> bool:
    owner, separator, repo = value.partition("/")
    return bool(owner and separator and repo and "/" not in repo)
