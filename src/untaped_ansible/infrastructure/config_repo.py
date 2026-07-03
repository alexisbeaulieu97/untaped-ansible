"""Config-file repositories for Ansible aliases and sources.

Aliases and sources are the tool-managed ``ansible`` *state*: the ``aliases``
map and ``sources`` list live under the top-level ``ansible`` section. Writes
go through the SDK's strict state wrappers rather than reaching into
config-file internals: the shared config file is co-owned by every untaped
tool, so a write must only touch this tool's section and never clobber
another's.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from untaped.api import ConfigError, StateCollection, StateMap, first_validation_error

from untaped_ansible.settings import SourceDefinition

_SECTION = "ansible"
_ALIASES_KEY = "aliases"
_SOURCES_KEY = "sources"
_ALIASES = StateMap(_SECTION, _ALIASES_KEY)
_SOURCES = StateCollection(_SECTION, _SOURCES_KEY, id_field="name")


class AliasRepository:
    """Read/write Ansible dependency aliases in ``~/.untaped/config.yml``."""

    def entries(self) -> dict[str, str]:
        return _ALIASES.entries()

    def set(self, alias: str, repo: str) -> None:
        _ALIASES.set(alias, repo)

    def remove(self, alias: str) -> bool:
        return _ALIASES.remove(alias)


class SourceRepository:
    """Read/write named repository sources in ``~/.untaped/config.yml``."""

    def entries(self) -> list[SourceDefinition]:
        return [_source_from_raw(raw) for raw in _SOURCES.entries()]

    def get(self, name: str) -> SourceDefinition | None:
        raw = _SOURCES.get(name)
        if raw is None:
            return None
        return _source_from_raw(raw)

    def upsert(self, source: SourceDefinition) -> None:
        _SOURCES.upsert(source.model_dump(exclude_none=True))

    def remove(self, name: str) -> bool:
        return _SOURCES.remove(name)


def _source_from_raw(raw: dict[str, Any]) -> SourceDefinition:
    try:
        return SourceDefinition.model_validate(raw)
    except ValidationError as exc:
        name = raw.get("name", "<unknown>")
        raise ConfigError(f"invalid source {name!r}: {first_validation_error(exc)}") from exc
