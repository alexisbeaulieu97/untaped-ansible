"""Parsers for Ansible role dependency declaration files."""

from __future__ import annotations

from typing import Any

import yaml

from untaped_ansible.domain.models import DependencyDeclaration, ParseReport


def parse_dependency_file(path: str, text: str) -> ParseReport:
    """Parse supported Ansible dependency files into role declarations."""
    data = _load_yaml(text)
    if data is None:
        return ParseReport()
    if path.endswith("meta/main.yml"):
        return ParseReport(dependencies=_parse_entries(data.get("dependencies"), path))
    if _is_requirements_path(path):
        if isinstance(data, list):
            return ParseReport(dependencies=_parse_entries(data, path))
        if isinstance(data, dict):
            return ParseReport(
                dependencies=_parse_entries(data.get("roles"), path),
                ignored_collections=_parse_collections(data.get("collections")),
            )
    return ParseReport()


def _load_yaml(text: str) -> Any:
    if not text.strip():
        return None
    data = yaml.safe_load(text)
    if isinstance(data, list | dict):
        return data
    return None


def _is_requirements_path(path: str) -> bool:
    return path.endswith("requirements.yml") or path.endswith("requirements.yaml")


def _parse_entries(raw: Any, source_path: str) -> tuple[DependencyDeclaration, ...]:
    if not isinstance(raw, list):
        return ()
    declarations: list[DependencyDeclaration] = []
    for entry in raw:
        declaration = _parse_entry(entry, source_path)
        if declaration is not None:
            declarations.append(declaration)
    return tuple(declarations)


def _parse_entry(entry: Any, source_path: str) -> DependencyDeclaration | None:
    if isinstance(entry, str):
        entry_name = entry.strip()
        if not entry_name:
            return None
        return DependencyDeclaration(name=entry_name, src=entry_name, source_path=source_path)
    if not isinstance(entry, dict):
        return None
    src = _string(entry.get("src"))
    name = _string(entry.get("name")) or _string(entry.get("role")) or _name_from_src(src)
    version = _string(entry.get("version"))
    if name is None and src is None:
        return None
    src_final = src or name
    name_final = name or src_final
    if name_final is None or src_final is None:
        return None
    return DependencyDeclaration(
        name=name_final,
        src=src_final,
        version=version,
        source_path=source_path,
    )


def _parse_collections(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            name = _string(entry.get("name")) or ""
        else:
            name = ""
        if name:
            names.append(name)
    return tuple(names)


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _name_from_src(src: str | None) -> str | None:
    if src is None:
        return None
    normalized = src.removeprefix("git+").removesuffix(".git").rstrip("/")
    return normalized.rsplit("/", maxsplit=1)[-1] or None
