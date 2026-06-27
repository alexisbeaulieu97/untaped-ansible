"""Repository target helpers shared by source refresh backends."""

from __future__ import annotations

from untaped_ansible.domain.payloads import ProbeTarget


def remote_url_for(target: ProbeTarget, clone_protocol: str) -> str:
    """Return the Git remote URL for a source repository target."""
    if clone_protocol == "ssh":
        return target.ssh_url or f"git@github.com:{target.full_name}.git"
    if target.clone_url:
        return target.clone_url
    if target.html_url:
        return f"{target.html_url.removesuffix('/')}.git"
    return f"https://github.com/{target.full_name}.git"
