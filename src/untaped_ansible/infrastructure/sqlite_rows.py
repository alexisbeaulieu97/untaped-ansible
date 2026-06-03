"""SQLite row mappers for dependency index payloads."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from untaped_ansible.domain.payloads import IndexedDependency, RefScanMetadata


def edge_from_row(row: sqlite3.Row) -> IndexedDependency:
    """Map a dependency edge row to its payload DTO."""
    return IndexedDependency(
        source_repo=row["source_repo"],
        source_ref=row["source_ref"],
        source_ref_kind=row["source_ref_kind"],
        source_sha=row["source_sha"],
        dependency_repo=row["dependency_repo"],
        dependency_name=row["dependency_name"],
        dependency_version=row["dependency_version"],
        source_path=row["source_path"],
        unresolved=row["unresolved"],
    )


def ref_scan_from_row(row: sqlite3.Row) -> RefScanMetadata:
    """Map a source ref scan row to its metadata payload."""
    return RefScanMetadata(
        source_key=row["source_key"],
        source_repo=row["source_repo"],
        ref_kind=row["ref_kind"],
        source_ref=row["source_ref"],
        source_sha=row["source_sha"],
        backend=row["backend"],
        clone_url=row["clone_url"],
        clone_protocol=row["clone_protocol"],
        dependency_paths_fingerprint=row["dependency_paths_fingerprint"],
        aliases_fingerprint=row["aliases_fingerprint"],
        checked_at=load_dt(row["checked_at"]),
        indexed_at=load_dt(row["indexed_at"]),
        last_error=row["last_error"],
    )


def dump_dt(value: datetime) -> str:
    """Serialize a timezone-aware datetime for SQLite storage."""
    return value.astimezone(UTC).isoformat()


def load_dt(value: str) -> datetime:
    """Deserialize a datetime stored by ``dump_dt``."""
    return datetime.fromisoformat(value)
