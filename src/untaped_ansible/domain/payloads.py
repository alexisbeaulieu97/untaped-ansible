"""Source-index payload DTOs shared across application and infrastructure."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IndexedDependency(BaseModel):
    """One indexed dependency edge from a scanned source repo/ref."""

    model_config = ConfigDict(frozen=True)

    source_repo: str
    source_ref: str | None
    source_ref_kind: str | None = None
    dependency_name: str
    source_path: str
    source_sha: str | None = None
    dependency_repo: str | None = None
    dependency_version: str | None = None
    unresolved: str | None = None


class GitRef(BaseModel):
    """Resolved Git ref selected for dependency indexing."""

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    sha: str


class ProbedRepo(BaseModel):
    """Refs and default branch reported by the freshness probe for one repo."""

    model_config = ConfigDict(frozen=True)

    default_branch: str | None = None
    refs: tuple[GitRef, ...] = ()


class ProbeReport(BaseModel):
    """Outcome of one batched ref freshness probe."""

    model_config = ConfigDict(frozen=True)

    repos: dict[str, ProbedRepo] = Field(default_factory=dict)
    failures: dict[str, str] = Field(default_factory=dict)
    rate_limit_cost: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None


class RepoFailure(BaseModel):
    """One source repository that failed during a refresh."""

    model_config = ConfigDict(frozen=True)

    repo: str
    reason: str


class RefreshProgressEvent(BaseModel):
    """Live progress for one source refresh phase."""

    model_config = ConfigDict(frozen=True)

    phase: Literal["expanding", "probing", "fetching"]
    done: int
    total: int
    changed: int | None = None


class CachedRef(BaseModel):
    """Cached ref metadata for graph display."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: str | None = None
    default_branch: str | None = None


class SourceRepoMetadata(BaseModel):
    """Cached metadata for one source repository."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_repo: str
    default_branch: str


class RefScanMetadata(BaseModel):
    """Freshness metadata for one indexed source repo/ref."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_repo: str
    ref_kind: str
    source_ref: str
    source_sha: str
    clone_url: str | None = None
    clone_protocol: str | None = None
    dependency_paths_fingerprint: str
    aliases_fingerprint: str = ""
    checked_at: datetime
    indexed_at: datetime


class RefScan(RefScanMetadata):
    """Replacement payload for one source repo/ref scan."""

    dependencies: tuple[IndexedDependency, ...] = ()


class RefScanTouch(BaseModel):
    """Freshness touch for one unchanged source repo/ref scan."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_repo: str
    ref_kind: str
    source_ref: str
    checked_at: datetime


class SourceIndexStatus(BaseModel):
    """Summary of one indexed source."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    scanned_at: datetime
    repos: int
    refs: int
    edges: int
