"""Config-file repositories for Ansible aliases and sources.

Aliases and sources are the tool-managed ``ansible`` *state*: the ``aliases``
map and ``sources`` list live under the top-level ``ansible`` section. Writes
go through the SDK's safe state surface (``mutate_tool_state`` /
``read_tool_state``) rather than reaching into config-file internals: the
shared config file is co-owned by every untaped tool, so a write must only
touch this tool's section and never clobber another's.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from untaped.api import ConfigError, first_validation_error
from untaped.config_file import mutate_tool_state, read_tool_state

from untaped_ansible.settings import SourceDefinition

_SECTION = "ansible"
_ALIASES_KEY = "aliases"
_SOURCES_KEY = "sources"


class AliasRepository:
    """Read/write Ansible dependency aliases in ``~/.untaped/config.yml``."""

    def entries(self) -> dict[str, str]:
        return _aliases(read_tool_state(_SECTION))

    def set(self, alias: str, repo: str) -> None:
        def _apply(state: dict[str, Any]) -> None:
            aliases = _aliases(state)
            aliases[alias] = repo
            state[_ALIASES_KEY] = aliases

        mutate_tool_state(_SECTION, _apply)

    def remove(self, alias: str) -> bool:
        removed = False

        def _apply(state: dict[str, Any]) -> None:
            nonlocal removed
            aliases = _aliases(state)
            if alias not in aliases:
                return
            del aliases[alias]
            removed = True
            if aliases:
                state[_ALIASES_KEY] = aliases
            else:
                # Drop the key when empty so the section can collapse.
                state.pop(_ALIASES_KEY, None)

        mutate_tool_state(_SECTION, _apply)
        return removed


class SourceRepository:
    """Read/write named repository sources in ``~/.untaped/config.yml``."""

    def entries(self) -> list[SourceDefinition]:
        return [_source_from_raw(raw) for raw in _source_rows(read_tool_state(_SECTION))]

    def get(self, name: str) -> SourceDefinition | None:
        for source in self.entries():
            if source.name == name:
                return source
        return None

    def upsert(self, source: SourceDefinition) -> None:
        def _apply(state: dict[str, Any]) -> None:
            sources = [row for row in _source_rows(state) if row.get("name") != source.name]
            sources.append(source.model_dump())
            state[_SOURCES_KEY] = sources

        mutate_tool_state(_SECTION, _apply)

    def remove(self, name: str) -> bool:
        removed = False

        def _apply(state: dict[str, Any]) -> None:
            nonlocal removed
            sources = _source_rows(state)
            new_sources = [row for row in sources if row.get("name") != name]
            removed = len(new_sources) != len(sources)
            if not removed:
                return
            if new_sources:
                state[_SOURCES_KEY] = new_sources
            else:
                # Drop the key when empty so the section can collapse.
                state.pop(_SOURCES_KEY, None)

        mutate_tool_state(_SECTION, _apply)
        return removed


def _aliases(state: dict[str, Any]) -> dict[str, str]:
    raw = state.get(_ALIASES_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(alias): str(repo) for alias, repo in raw.items()}


def _source_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get(_SOURCES_KEY)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _source_from_raw(raw: dict[str, Any]) -> SourceDefinition:
    try:
        return SourceDefinition.model_validate(raw)
    except ValidationError as exc:
        name = raw.get("name", "<unknown>")
        raise ConfigError(f"invalid source {name!r}: {first_validation_error(exc)}") from exc
