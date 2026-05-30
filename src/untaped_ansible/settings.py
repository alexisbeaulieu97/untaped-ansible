"""Settings and state models contributed by the Ansible plugin."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_DEPENDENCY_PATHS = (
    "roles/requirements.yml",
    "roles/requirements.yaml",
    "requirements.yml",
    "requirements.yaml",
    "meta/requirements.yml",
    "meta/requirements.yaml",
    "meta/main.yml",
)


class SourceDefinition(BaseModel):
    """Named GitHub search boundary for index refresh and impact queries."""

    name: str
    orgs: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    dependency_paths: list[str] = Field(default_factory=list)
    ref_kinds: list[str] = Field(default_factory=lambda: ["heads", "tags"])
    ref_patterns: list[str] = Field(default_factory=list)


class AnsibleSettings(BaseModel):
    """User-tunable profile settings."""

    index_path: Path = Path("~/.untaped/ansible-index.sqlite3")
    stale_after: int = 86_400
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
