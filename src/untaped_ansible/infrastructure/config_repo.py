"""Config-file repositories for Ansible aliases and scopes."""

from __future__ import annotations

from typing import Any

from untaped.config_file import (
    get_at_path,
    mutate_config,
    read_config_dict,
    set_at_path,
    unset_at_path,
)

from untaped_ansible.settings import ScopeDefinition

_ALIASES_PATH: tuple[str, ...] = ("ansible", "aliases")
_SCOPES_PATH: tuple[str, ...] = ("ansible", "scopes")


class AliasRepository:
    """Read/write Ansible dependency aliases in ``~/.untaped/config.yml``."""

    def entries(self) -> dict[str, str]:
        raw = get_at_path(read_config_dict(), _ALIASES_PATH)
        if not isinstance(raw, dict):
            return {}
        return {str(alias): str(repo) for alias, repo in raw.items()}

    def set(self, alias: str, repo: str) -> None:
        def _apply(data: dict[str, Any]) -> None:
            aliases = _aliases(data)
            aliases[alias] = repo
            set_at_path(data, _ALIASES_PATH, aliases)

        mutate_config(_apply)

    def remove(self, alias: str) -> bool:
        removed = False

        def _apply(data: dict[str, Any]) -> None:
            nonlocal removed
            aliases = _aliases(data)
            if alias not in aliases:
                return
            del aliases[alias]
            removed = True
            if aliases:
                set_at_path(data, _ALIASES_PATH, aliases)
            else:
                unset_at_path(data, _ALIASES_PATH)

        mutate_config(_apply)
        return removed


class ScopeRepository:
    """Read/write named repository scopes in ``~/.untaped/config.yml``."""

    def entries(self) -> list[ScopeDefinition]:
        return [_scope_from_raw(raw) for raw in _scope_rows(read_config_dict())]

    def get(self, name: str) -> ScopeDefinition | None:
        for scope in self.entries():
            if scope.name == name:
                return scope
        return None

    def upsert(self, scope: ScopeDefinition) -> None:
        def _apply(data: dict[str, Any]) -> None:
            scopes = [row for row in _scope_rows(data) if row.get("name") != scope.name]
            scopes.append(scope.model_dump())
            set_at_path(data, _SCOPES_PATH, scopes)

        mutate_config(_apply)

    def remove(self, name: str) -> bool:
        removed = False

        def _apply(data: dict[str, Any]) -> None:
            nonlocal removed
            scopes = _scope_rows(data)
            new_scopes = [row for row in scopes if row.get("name") != name]
            removed = len(new_scopes) != len(scopes)
            if not removed:
                return
            if new_scopes:
                set_at_path(data, _SCOPES_PATH, new_scopes)
            else:
                unset_at_path(data, _SCOPES_PATH)

        mutate_config(_apply)
        return removed


def _aliases(data: dict[str, Any]) -> dict[str, str]:
    raw = get_at_path(data, _ALIASES_PATH)
    if not isinstance(raw, dict):
        return {}
    return {str(alias): str(repo) for alias, repo in raw.items()}


def _scope_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = get_at_path(data, _SCOPES_PATH)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _scope_from_raw(raw: dict[str, Any]) -> ScopeDefinition:
    return ScopeDefinition.model_validate(raw)
