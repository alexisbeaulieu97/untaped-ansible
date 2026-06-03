"""Tests for human Git ref display ordering."""

from __future__ import annotations

from untaped_ansible.domain.ref_display import RefDisplay, sort_ref_displays


def test_sort_ref_displays_prioritizes_default_branch_then_other_branches() -> None:
    refs = (
        RefDisplay("v3.0.0", kind="heads", default_branch="trunk"),
        RefDisplay("main", kind="tags", default_branch="trunk"),
        RefDisplay("trunk", kind="heads", default_branch="trunk"),
        RefDisplay("feature/10", kind="heads", default_branch="trunk"),
        RefDisplay("feature/2", kind="heads", default_branch="trunk"),
        RefDisplay("docs", default_branch="trunk"),
    )

    assert [ref.name for ref in sort_ref_displays(refs)] == [
        "trunk",
        "feature/2",
        "feature/10",
        "v3.0.0",
        "main",
        "docs",
    ]


def test_sort_ref_displays_orders_semver_tags_newest_first() -> None:
    refs = (
        RefDisplay("release", kind="tags"),
        RefDisplay("v2.0.0-alpha.2", kind="tags"),
        RefDisplay("v1.10.0", kind="tags"),
        RefDisplay("v2.0.0", kind="tags"),
        RefDisplay("v2.0.0-alpha.10", kind="tags"),
        RefDisplay("v2.0.0-rc.1", kind="tags"),
        RefDisplay("v1.2", kind="tags"),
    )

    assert [ref.name for ref in sort_ref_displays(refs)] == [
        "v2.0.0",
        "v2.0.0-rc.1",
        "v2.0.0-alpha.10",
        "v2.0.0-alpha.2",
        "v1.10.0",
        "release",
        "v1.2",
    ]
