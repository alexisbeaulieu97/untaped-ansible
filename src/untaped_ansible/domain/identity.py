"""Canonical identity resolution for Ansible dependency declarations."""

from __future__ import annotations

import re

from untaped_ansible.domain.models import DependencyDeclaration, ResolvedDependency

_HTTPS_RE = re.compile(r"^(?:git\+)?https://github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_SSH_RE = re.compile(r"^git@github\.com:(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$")
_SSH_URL_RE = re.compile(r"^ssh://git@github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class IdentityResolver:
    """Resolve dependency declarations to canonical GitHub ``owner/repo`` ids."""

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = aliases or {}

    def resolve(self, declaration: DependencyDeclaration) -> ResolvedDependency:
        key = declaration.src or declaration.name
        repo = (
            self._aliases.get(key) or self._aliases.get(declaration.name) or _repo_from_source(key)
        )
        if repo is not None:
            return ResolvedDependency(declaration=declaration, repo=repo)
        return ResolvedDependency(declaration=declaration, unresolved=key)


def _repo_from_source(source: str) -> str | None:
    value = source.strip()
    if _OWNER_REPO_RE.fullmatch(value):
        return value
    for pattern in (_HTTPS_RE, _SSH_RE, _SSH_URL_RE):
        match = pattern.fullmatch(value)
        if match is not None:
            return match.group("repo").removesuffix(".git")
    return None
