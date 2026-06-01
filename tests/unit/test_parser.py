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
    assert parse_dependency_file("roles/requirements.yml", "").dependencies == ()


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
