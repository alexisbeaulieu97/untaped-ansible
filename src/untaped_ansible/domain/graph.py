"""Graph model for Ansible dependency and impact analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EdgeRelation = Literal["requires", "impacts"]


class GraphNode(BaseModel):
    """One graph node, resolved or unresolved."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    repo: str | None = None
    ref: str | None = None
    ref_kind: str | None = Field(default=None, exclude=True)
    default_branch: str | None = Field(default=None, exclude=True)
    kind: str = "role"
    unresolved: str | None = None


class GraphEdge(BaseModel):
    """One directed graph edge."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    target_id: str
    relation: EdgeRelation
    source_path: str | None = None
    version: str | None = None


class DependencyGraph(BaseModel):
    """Resolved graph plus warnings for lossy or stale data."""

    model_config = ConfigDict(frozen=True)

    target_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...] = ()
    warnings: tuple[str, ...] = ()
