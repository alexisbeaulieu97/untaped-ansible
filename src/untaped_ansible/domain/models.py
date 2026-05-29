"""Pure domain models for Ansible dependency graphing."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DependencyDeclaration(BaseModel):
    """One role dependency declaration found in an Ansible YAML file."""

    model_config = ConfigDict(frozen=True)

    name: str
    src: str
    version: str | None = None
    source_path: str


class ResolvedDependency(BaseModel):
    """A dependency declaration resolved to a canonical repo or kept unresolved."""

    model_config = ConfigDict(frozen=True)

    declaration: DependencyDeclaration
    repo: str | None = None
    unresolved: str | None = None


class ParseReport(BaseModel):
    """Parsed dependencies plus ignored non-v1 declarations."""

    model_config = ConfigDict(frozen=True)

    dependencies: tuple[DependencyDeclaration, ...] = ()
    ignored_collections: tuple[str, ...] = ()
