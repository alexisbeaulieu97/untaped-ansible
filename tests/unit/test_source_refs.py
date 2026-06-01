"""Tests for effective source ref selection."""

from __future__ import annotations

from untaped_ansible.application.source_refs import source_ref_selections
from untaped_ansible.settings import SourceDefinition


def test_source_ref_selections_default_to_all_heads_and_tags() -> None:
    selections = source_ref_selections(
        SourceDefinition(name="prod", repos=["acme/site"]),
        default_branch="master",
        ref_scan_default="all",
    )

    assert [
        (selection.kind, selection.patterns, selection.namespaces) for selection in selections
    ] == [
        ("heads", ("*",), ("heads",)),
        ("tags", ("*",), ("tags",)),
    ]


def test_source_ref_selections_can_default_to_default_branch_only() -> None:
    selections = source_ref_selections(
        SourceDefinition(name="prod", repos=["acme/site"]),
        default_branch="master",
        ref_scan_default="default_branch",
    )

    assert [
        (selection.kind, selection.patterns, selection.namespaces) for selection in selections
    ] == [
        ("heads", ("master",), ("heads/master",)),
    ]


def test_source_ref_selections_use_patterns_for_heads_and_tags_when_kind_is_omitted() -> None:
    selections = source_ref_selections(
        SourceDefinition(name="prod", repos=["acme/site"], ref_patterns=["v3"]),
        default_branch="master",
        ref_scan_default="all",
    )

    assert [
        (selection.kind, selection.patterns, selection.namespaces) for selection in selections
    ] == [
        ("heads", ("v3",), ("heads/v3",)),
        ("tags", ("v3",), ("tags/v3",)),
    ]


def test_source_ref_selections_scan_all_selected_kind_without_pattern() -> None:
    selections = source_ref_selections(
        SourceDefinition(name="prod", repos=["acme/site"], ref_kinds=["tags"]),
        default_branch="master",
        ref_scan_default="all",
    )

    assert [
        (selection.kind, selection.patterns, selection.namespaces) for selection in selections
    ] == [
        ("tags", ("*",), ("tags",)),
    ]


def test_source_ref_selections_use_explicit_kind_and_pattern() -> None:
    selections = source_ref_selections(
        SourceDefinition(
            name="prod",
            repos=["acme/site"],
            ref_kinds=["heads"],
            ref_patterns=["release/*"],
        ),
        default_branch="master",
        ref_scan_default="all",
    )

    assert [
        (selection.kind, selection.patterns, selection.namespaces) for selection in selections
    ] == [
        ("heads", ("release/*",), ("heads/release/",)),
    ]
