"""Shared payloads for source index refresh use cases."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from untaped_ansible.domain.payloads import RepoFailure


class RefreshResult(BaseModel):
    """Summary of an index refresh."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    completed: bool = True
    pause_reason: str | None = None
    repos: int
    refs: int
    edges: int
    ignored_collections: tuple[str, ...] = ()
    changed_refs: int = 0
    unchanged_refs: int = 0
    failures: tuple[RepoFailure, ...] = ()
    rate_limit_cost: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
