"""Source-index payload DTOs shared across application and infrastructure."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IndexedDependency(BaseModel):
    """One indexed dependency edge from a scanned source repo/ref."""

    model_config = ConfigDict(frozen=True)

    source_repo: str
    source_ref: str | None
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


class RefScanMetadata(BaseModel):
    """Freshness metadata for one indexed source repo/ref."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_repo: str
    ref_kind: str
    source_ref: str
    source_sha: str
    backend: str
    clone_url: str | None = None
    clone_protocol: str | None = None
    dependency_paths_fingerprint: str
    aliases_fingerprint: str = ""
    checked_at: datetime
    indexed_at: datetime
    last_error: str | None = None


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


class IndexScan(BaseModel):
    """Complete replacement payload for one source scan."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    scanned_at: datetime
    repos: int | None = None
    refs: int | None = None
    dependencies: tuple[IndexedDependency, ...] = ()
