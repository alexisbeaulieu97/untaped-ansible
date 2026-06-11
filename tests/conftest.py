"""Shared fixtures: mirror untaped core's plugin startup for direct app tests.

Production registers every plugin manifest (config sections included) before
dispatching a command. Tests invoke the Cyclopts app directly, so the same
registration must happen here for ``plugin_context().section(...)`` reads.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from untaped.api import PluginRegistry
from untaped.plugins import discover_plugins, register_plugins
from untaped.settings import get_settings

# Discovery (not single-plugin registration): the ansible CLI also reads the
# `github` section contributed by the untaped-github plugin in this venv.
_registry = PluginRegistry()
register_plugins(_registry, discover_plugins(_registry))
if _registry.load_errors:
    raise RuntimeError(f"plugin registration failed: {_registry.load_errors}")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings are cached per process; tests swap UNTAPED_CONFIG freely."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
