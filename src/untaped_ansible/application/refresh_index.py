"""Shared payloads for source index refresh use cases."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RefreshResult(BaseModel):
    """Summary of an index refresh."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    repos: int
    refs: int
    edges: int
    ignored_collections: tuple[str, ...] = ()
    changed_refs: int = 0
    unchanged_refs: int = 0
