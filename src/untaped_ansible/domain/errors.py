"""Errors shared across the application and infrastructure layers."""

from __future__ import annotations


class GitCacheError(RuntimeError):
    """Raised when local Git cache operations fail."""
