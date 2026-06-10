"""untaped-ansible: Ansible dependency graphing plugin."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cyclopts import App

__all__ = ["app"]


def __getattr__(name: str) -> App:
    """Lazily re-export the Cyclopts app (PEP 562).

    Plugin discovery imports this package; the CLI tree must only load when
    the ``ansible`` command is actually dispatched or ``app`` is accessed.
    """
    if name == "app":
        # Deferring this import is the point: eager CLI imports would defeat
        # the manifest's lazy CliSpec(import_path=...).
        from untaped_ansible.cli import app  # noqa: PLC0415

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
