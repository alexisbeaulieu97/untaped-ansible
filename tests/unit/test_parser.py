"""Tests for parsing Ansible role dependency declarations."""

from __future__ import annotations

from untaped_ansible.domain.parser import parse_dependency_file


def test_parse_requirements_roles_from_list_entries() -> None:
    report = parse_dependency_file(
        "roles/requirements.yml",
        """
        - src: https://github.com/acme/base
          version: v1.2.3
          name: base_role
        - geerlingguy.apache
        """,
    )

    assert [(dep.name, dep.src, dep.version) for dep in report.dependencies] == [
        ("base_role", "https://github.com/acme/base", "v1.2.3"),
        ("geerlingguy.apache", "geerlingguy.apache", None),
    ]
    assert report.ignored_collections == ()


def test_parse_requirements_roles_from_roles_key_and_reports_collections() -> None:
    report = parse_dependency_file(
        "requirements.yml",
        """
        roles:
          - src: git+https://github.com/acme/users.git
            version: main
        collections:
          - name: community.general
        """,
    )

    assert [(dep.name, dep.src, dep.version) for dep in report.dependencies] == [
        ("users", "git+https://github.com/acme/users.git", "main")
    ]
    assert report.ignored_collections == ("community.general",)


def test_parse_meta_main_dependencies_from_simple_and_complex_entries() -> None:
    report = parse_dependency_file(
        "meta/main.yml",
        """
        dependencies:
          - common
          - role: apache
            vars:
              port: 80
          - name: composer
            src: git+https://github.com/acme/composer.git
            version: 7753962
        """,
    )

    assert [(dep.name, dep.src, dep.version) for dep in report.dependencies] == [
        ("common", "common", None),
        ("apache", "apache", None),
        ("composer", "git+https://github.com/acme/composer.git", "7753962"),
    ]


def test_parse_empty_or_unknown_yaml_shape_returns_empty_report() -> None:
    assert parse_dependency_file("README.yml", "name: not a dependency file").dependencies == ()
    report = parse_dependency_file("roles/requirements.yml", "")
    assert report.dependencies == ()
    assert report.warnings == ()


def test_parse_yaml_none_document_returns_empty_report_without_warning() -> None:
    report = parse_dependency_file("meta/main.yml", "---\n")

    assert report.dependencies == ()
    assert report.ignored_collections == ()
    assert report.warnings == ()


def test_parse_invalid_templated_yaml_returns_empty_report() -> None:
    report = parse_dependency_file(
        "meta/main.yml",
        """
        ---
        galaxy_info:
          role_name: {@ role_slug @}
        """,
    )

    assert report.dependencies == ()
    assert report.ignored_collections == ()
    assert [(warning.source_path, warning.reason) for warning in report.warnings] == [
        ("meta/main.yml", "could not parse dependency YAML")
    ]


def test_parse_meta_main_list_shape_returns_warning_instead_of_crashing() -> None:
    report = parse_dependency_file("meta/main.yml", "- common\n")

    assert report.dependencies == ()
    assert report.ignored_collections == ()
    assert [(warning.source_path, warning.reason) for warning in report.warnings] == [
        ("meta/main.yml", "expected mapping at top level")
    ]


def test_parse_recognized_scalar_yaml_returns_warning() -> None:
    report = parse_dependency_file("requirements.yml", "42\n")

    assert report.dependencies == ()
    assert report.ignored_collections == ()
    assert [(warning.source_path, warning.reason) for warning in report.warnings] == [
        ("requirements.yml", "expected mapping or list at top level")
    ]


def test_parse_null_or_empty_dependency_sections_are_warning_free() -> None:
    meta_report = parse_dependency_file("meta/main.yml", "dependencies:\n")
    requirements_report = parse_dependency_file(
        "requirements.yml",
        """
        roles: []
        collections:
        """,
    )

    assert meta_report.dependencies == ()
    assert meta_report.warnings == ()
    assert requirements_report.dependencies == ()
    assert requirements_report.ignored_collections == ()
    assert requirements_report.warnings == ()


def test_parse_wrong_shaped_nested_dependency_sections_warn() -> None:
    meta_report = parse_dependency_file(
        "meta/main.yml",
        """
        dependencies:
          common: {}
        """,
    )
    invalid_roles_report = parse_dependency_file(
        "requirements.yml",
        """
        roles: "{{ roles }}"
        collections:
          - name: community.general
        """,
    )
    invalid_collections_report = parse_dependency_file(
        "requirements.yml",
        """
        roles:
          - src: https://github.com/acme/base
        collections: community.general
        """,
    )

    assert meta_report.dependencies == ()
    assert [(warning.source_path, warning.reason) for warning in meta_report.warnings] == [
        ("meta/main.yml", "expected list at dependencies")
    ]
    assert invalid_roles_report.dependencies == ()
    assert invalid_roles_report.ignored_collections == ("community.general",)
    assert [(warning.source_path, warning.reason) for warning in invalid_roles_report.warnings] == [
        ("requirements.yml", "expected list at roles")
    ]
    assert [
        (dep.name, dep.src, dep.version) for dep in invalid_collections_report.dependencies
    ] == [("base", "https://github.com/acme/base", None)]
    assert invalid_collections_report.ignored_collections == ()
    assert [
        (warning.source_path, warning.reason) for warning in invalid_collections_report.warnings
    ] == [("requirements.yml", "expected list at collections")]
