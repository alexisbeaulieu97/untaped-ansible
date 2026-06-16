"""Shared fixtures for the untaped-ansible test suite.

untaped-ansible is a standalone SDK tool: command code reads its own
``ansible`` section and the foreign ``github`` section with
``get_config_section``, which builds a one-off model for an unregistered
section. So direct ``app`` invocations need no section registration —
only a clean settings cache per test, since settings are cached per process
and tests swap ``UNTAPED_CONFIG`` freely.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from untaped.settings import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings are cached per process; tests swap UNTAPED_CONFIG freely."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
