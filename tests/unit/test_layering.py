"""Architecture guard tests for the Ansible tool's DDD layers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "untaped_ansible"


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return (
            test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        )
    return False


def _typecheck_block_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            for stmt in node.body:
                for child in ast.walk(stmt):
                    if hasattr(child, "lineno"):
                        lines.add(child.lineno)
    return lines


def _runtime_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    typecheck_block_lines = _typecheck_block_lines(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and node.lineno not in typecheck_block_lines
    ]


def _violations_in_file(
    py_file: Path,
    source_dir: Path,
    forbidden_subpackage: str,
) -> list[str]:
    forbidden_root = f"untaped_ansible.{forbidden_subpackage}"
    rel = py_file.relative_to(SRC_ROOT)
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    found: list[str] = []
    for imp in _runtime_imports(tree):
        if isinstance(imp, ast.Import):
            bad = [
                alias.name
                for alias in imp.names
                if alias.name == forbidden_root or alias.name.startswith(f"{forbidden_root}.")
            ]
            if bad:
                found.append(f"{rel}:{imp.lineno} imports {', '.join(bad)}")
        elif imp.level > 0:
            module = imp.module or ""
            if module == forbidden_subpackage or module.startswith(f"{forbidden_subpackage}."):
                found.append(f"{rel}:{imp.lineno} imports {'.' * imp.level}{module}")
        elif imp.module and (
            imp.module == forbidden_root or imp.module.startswith(f"{forbidden_root}.")
        ):
            found.append(f"{rel}:{imp.lineno} imports {imp.module}")
    return found


@pytest.mark.parametrize(
    ("source_dir", "forbidden_subpackage"),
    [
        (SRC_ROOT / "application", "infrastructure"),
        (SRC_ROOT / "infrastructure", "application"),
    ],
    ids=["application->infrastructure", "infrastructure->application"],
)
def test_layers_do_not_import_forbidden_siblings(
    source_dir: Path,
    forbidden_subpackage: str,
) -> None:
    violations: list[str] = []
    for py_file in sorted(source_dir.rglob("*.py")):
        violations.extend(_violations_in_file(py_file, source_dir, forbidden_subpackage))

    assert not violations, (
        f"{source_dir.relative_to(SRC_ROOT)} must not import "
        f"untaped_ansible.{forbidden_subpackage} at runtime "
        "(TYPE_CHECKING imports are fine):\n  " + "\n  ".join(violations)
    )
