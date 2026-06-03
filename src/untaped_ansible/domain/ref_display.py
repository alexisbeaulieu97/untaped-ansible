"""Human display ordering for Git refs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cmp_to_key


@dataclass(frozen=True, slots=True)
class RefDisplay:
    """Git ref metadata needed for human display sorting."""

    name: str
    kind: str | None = None
    default_branch: str | None = None


def sort_ref_displays(refs: Iterable[RefDisplay]) -> list[RefDisplay]:
    """Sort refs for the human tree report."""
    return sorted(refs, key=cmp_to_key(_compare_refs))


def compare_ref_displays(left: RefDisplay, right: RefDisplay) -> int:
    """Compare two refs using tree report display order."""
    return _compare_refs(left, right)


def natural_compare(left: str, right: str) -> int:
    """Case-insensitive natural string comparison."""
    left_tokens = _natural_tokens(left)
    right_tokens = _natural_tokens(right)
    for left_token, right_token in zip(left_tokens, right_tokens, strict=False):
        if left_token == right_token:
            continue
        return -1 if left_token < right_token else 1
    if len(left_tokens) == len(right_tokens):
        return 0
    return -1 if len(left_tokens) < len(right_tokens) else 1


_SEMVER_RE = re.compile(
    r"^v?"
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True, slots=True)
class _SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]


def _compare_refs(left: RefDisplay, right: RefDisplay) -> int:
    left_rank = _kind_rank(left.kind)
    right_rank = _kind_rank(right.kind)
    if left_rank != right_rank:
        return -1 if left_rank < right_rank else 1
    if left_rank == 0:
        default_cmp = _compare_default_branch(left, right)
        if default_cmp != 0:
            return default_cmp
        return natural_compare(left.name, right.name)
    if left_rank == 1:
        return _compare_tags(left.name, right.name)
    return natural_compare(left.name, right.name)


def _kind_rank(kind: str | None) -> int:
    if kind == "heads":
        return 0
    if kind == "tags":
        return 1
    return 2


def _compare_default_branch(left: RefDisplay, right: RefDisplay) -> int:
    left_is_default = left.default_branch is not None and left.name == left.default_branch
    right_is_default = right.default_branch is not None and right.name == right.default_branch
    if left_is_default == right_is_default:
        return 0
    return -1 if left_is_default else 1


def _compare_tags(left: str, right: str) -> int:
    left_semver = _parse_semver(left)
    right_semver = _parse_semver(right)
    if left_semver is not None and right_semver is not None:
        semver_cmp = _compare_semver(left_semver, right_semver)
        if semver_cmp != 0:
            return -semver_cmp
        return natural_compare(left, right)
    if left_semver is not None:
        return -1
    if right_semver is not None:
        return 1
    return natural_compare(left, right)


def _parse_semver(value: str) -> _SemVer | None:
    match = _SEMVER_RE.match(value)
    if match is None:
        return None
    prerelease = match.group(4)
    return _SemVer(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        prerelease=tuple(prerelease.split(".")) if prerelease else (),
    )


def _compare_semver(left: _SemVer, right: _SemVer) -> int:
    left_version = (left.major, left.minor, left.patch)
    right_version = (right.major, right.minor, right.patch)
    if left_version != right_version:
        return -1 if left_version < right_version else 1
    return _compare_prerelease(left.prerelease, right.prerelease)


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for left_identifier, right_identifier in zip(left, right, strict=False):
        identifier_cmp = _compare_prerelease_identifier(left_identifier, right_identifier)
        if identifier_cmp != 0:
            return identifier_cmp
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def _compare_prerelease_identifier(left: str, right: str) -> int:
    left_is_numeric = left.isdigit()
    right_is_numeric = right.isdigit()
    if left_is_numeric and right_is_numeric:
        left_number = int(left)
        right_number = int(right)
        if left_number == right_number:
            return 0
        return -1 if left_number < right_number else 1
    if left_is_numeric != right_is_numeric:
        return -1 if left_is_numeric else 1
    if left == right:
        return 0
    return -1 if left < right else 1


def _natural_tokens(value: str) -> tuple[tuple[int, int | str], ...]:
    tokens: list[tuple[int, int | str]] = []
    for token in re.split(r"(\d+)", value.lower()):
        if not token:
            continue
        if token.isdigit():
            tokens.append((0, int(token)))
        else:
            tokens.append((1, token))
    return tuple(tokens)
