"""Graph model for Ansible dependency and impact analysis."""

from __future__ import annotations

from hashlib import sha256
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
    unresolved: str | None = None


class GraphEdge(BaseModel):
    """One directed graph edge."""

    model_config = ConfigDict(frozen=True)

    id: str = ""
    source_id: str
    target_id: str
    relation: EdgeRelation
    source_path: str | None = None
    version: str | None = None

    def model_post_init(self, __context: object) -> None:
        if self.id:
            return
        digest = sha256(
            f"{self.relation}\0{self.source_id}\0{self.target_id}".encode()
        ).hexdigest()[:16]
        object.__setattr__(self, "id", f"edge:{digest}")


class GraphCycle(BaseModel):
    """One directed cycle detected in the emitted graph."""

    model_config = ConfigDict(frozen=True)

    direction: EdgeRelation
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


class DependencyGraph(BaseModel):
    """Resolved graph plus warnings for lossy or stale data."""

    model_config = ConfigDict(frozen=True)

    target_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...] = ()
    cycles: tuple[GraphCycle, ...] = ()
    warnings: tuple[str, ...] = ()
