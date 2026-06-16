"""untaped-ansible: Ansible dependency graph CLI built on the untaped SDK."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cyclopts import App

__all__ = ["app"]


def __getattr__(name: str) -> App:
    """Lazily re-export the Cyclopts app (PEP 562).

    Deferring the CLI import keeps the command tree off the import path until
    ``app`` is actually accessed.
    """
    if name == "app":
        from untaped_ansible.cli import app  # noqa: PLC0415

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
