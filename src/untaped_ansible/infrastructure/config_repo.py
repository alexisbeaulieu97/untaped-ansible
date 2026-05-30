"""Config-file repositories for Ansible aliases and sources."""

from __future__ import annotations

from typing import Any

from untaped.config_file import (
    get_at_path,
    mutate_config,
    read_config_dict,
    set_at_path,
    unset_at_path,
)

from untaped_ansible.settings import SourceDefinition

_ALIASES_PATH: tuple[str, ...] = ("ansible", "aliases")
_SOURCES_PATH: tuple[str, ...] = ("ansible", "sources")


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


class SourceRepository:
    """Read/write named repository sources in ``~/.untaped/config.yml``."""

    def entries(self) -> list[SourceDefinition]:
        return [_source_from_raw(raw) for raw in _source_rows(read_config_dict())]

    def get(self, name: str) -> SourceDefinition | None:
        for source in self.entries():
            if source.name == name:
                return source
        return None

    def upsert(self, source: SourceDefinition) -> None:
        def _apply(data: dict[str, Any]) -> None:
            sources = [row for row in _source_rows(data) if row.get("name") != source.name]
            sources.append(source.model_dump())
            set_at_path(data, _SOURCES_PATH, sources)

        mutate_config(_apply)

    def remove(self, name: str) -> bool:
        removed = False

        def _apply(data: dict[str, Any]) -> None:
            nonlocal removed
            sources = _source_rows(data)
            new_sources = [row for row in sources if row.get("name") != name]
            removed = len(new_sources) != len(sources)
            if not removed:
                return
            if new_sources:
                set_at_path(data, _SOURCES_PATH, new_sources)
            else:
                unset_at_path(data, _SOURCES_PATH)

        mutate_config(_apply)
        return removed


def _aliases(data: dict[str, Any]) -> dict[str, str]:
    raw = get_at_path(data, _ALIASES_PATH)
    if not isinstance(raw, dict):
        return {}
    return {str(alias): str(repo) for alias, repo in raw.items()}


def _source_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = get_at_path(data, _SOURCES_PATH)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _source_from_raw(raw: dict[str, Any]) -> SourceDefinition:
    return SourceDefinition.model_validate(raw)
