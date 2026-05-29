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


class ScopeDefinition(BaseModel):
    """Named repository/ref scope for index refresh and impact queries."""

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

    scopes: list[ScopeDefinition] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
