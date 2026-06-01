"""Effective source ref selection for dependency source refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal

from untaped_ansible.settings import SourceDefinition

RefScanDefault = Literal["all", "default_branch"]


@dataclass(frozen=True)
class SourceRefSelection:
    """One ref kind and matching strategy selected for source refresh."""

    kind: str
    patterns: tuple[str, ...]
    namespaces: tuple[str, ...]


def source_ref_selections(
    source: SourceDefinition,
    *,
    default_branch: str,
    ref_scan_default: RefScanDefault,
) -> list[SourceRefSelection]:
    """Resolve source ref settings into concrete ref namespaces and filters."""
    if ref_scan_default not in {"all", "default_branch"}:
        raise ValueError("ref_scan_default must be 'all' or 'default_branch'")
    kinds = tuple(source.ref_kinds)
    patterns = tuple(source.ref_patterns)
    if not kinds:
        kinds = (
            ("heads",)
            if not patterns and ref_scan_default == "default_branch"
            else ("heads", "tags")
        )
    if not patterns:
        patterns = (
            (default_branch,)
            if not source.ref_kinds and ref_scan_default == "default_branch"
            else ("*",)
        )
    return [
        SourceRefSelection(
            kind=kind,
            patterns=patterns,
            namespaces=tuple(matching_ref_namespaces(kind, patterns)),
        )
        for kind in kinds
    ]


def matching_ref_namespaces(kind: str, patterns: tuple[str, ...]) -> list[str]:
    if not patterns:
        return [kind]
    namespaces: list[str] = []
    for pattern in patterns:
        literal_prefix = _safe_literal_ref_prefix(pattern)
        namespace = kind if literal_prefix == "" else f"{kind}/{literal_prefix}"
        if namespace == kind:
            return [kind]
        if any(namespace.startswith(existing) for existing in namespaces):
            continue
        namespaces = [existing for existing in namespaces if not existing.startswith(namespace)]
        namespaces.append(namespace)
    return namespaces


def pattern_matches(value: str, patterns: tuple[str, ...]) -> bool:
    return not patterns or any(fnmatch(value, pattern) for pattern in patterns)


def _safe_literal_ref_prefix(pattern: str) -> str:
    if not _has_wildcard(pattern):
        return pattern
    prefix = _literal_ref_prefix(pattern)
    slash = prefix.rfind("/")
    if slash == -1:
        return ""
    return prefix[: slash + 1]


def _literal_ref_prefix(pattern: str) -> str:
    wildcard_positions = [
        position for token in ("*", "?", "[") if (position := pattern.find(token)) != -1
    ]
    if not wildcard_positions:
        return pattern
    return pattern[: min(wildcard_positions)]


def _has_wildcard(pattern: str) -> bool:
    return any(token in pattern for token in ("*", "?", "["))
