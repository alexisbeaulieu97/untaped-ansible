"""Contract tests for the PyPI/TestPyPI release workflow (Option-C canonical).

Adopting a tool: copy this file to tests/unit/test_release_workflow.py and edit ONLY the
PER-TOOL CONFIG block. Everything below the marker must stay byte-identical to
core .github/release/templates/test_release_workflow.py.tmpl — diff before merging a release PR.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

# ============================ PER-TOOL CONFIG ============================
# The ONLY block that varies between tools.
DIST_NAME = "untaped-ansible"
CONSOLE_SCRIPT = "untaped-ansible"
EXPECTED_VERSION = "0.11.1"
# Internal untaped-ecosystem deps, as (PEP 508 requirement, uv-source rev or None).
#   rev = "vX.Y.Z" when the dep is git-sourced in [tool.uv.sources].
#   None when it installs from PyPI.
INTERNAL_DEPS: list[tuple[str, str | None]] = [
    ("untaped>=2.4.4,<3", "v2.4.4"),
    ("untaped-github>=0.12.2", None),
]
# Docs that must steer users to PyPI install (repo-relative paths); [] to skip the docs check.
PYPI_INSTALL_DOCS: list[str] = [
    "README.md",
]
# ========================================================================

CORE_RELEASE_TOOL_SHA = "07116cc11d4217283ad42badea4f5d5744542f2a"
EXPECTED_UV_VERSION = "0.11.26"
EXPECTED_ACTION_REFS = {
    "actions/checkout": "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
    "actions/cache": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "astral-sh/setup-uv": "fac544c07dec837d0ccb6301d7b5580bf5edae39",
    "pypa/gh-action-pypi-publish": "cef221092ed1bacb1cc03d23a2d87d1d172e277b",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s#]+)(?:\s+#.*)?\s*$", re.MULTILINE)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_workflow_text())


def _step_block(name: str) -> str:
    text = _workflow_text()
    next_step_or_job = r"(?=^      - name: |^  [a-zA-Z0-9_-]+:|\Z)"
    pattern = rf"(?ms)^      - name: {re.escape(name)}\n.*?{next_step_or_job}"
    match = re.search(pattern, text)
    assert match is not None, f"workflow step not found: {name}"
    return match.group(0)


def _workflow_steps(job_name: str) -> list[dict[str, Any]]:
    return list(_workflow()["jobs"][job_name]["steps"])


def _workflow_step(job_name: str, name: str) -> dict[str, Any]:
    for step in _workflow_steps(job_name):
        if step["name"] == name:
            return step
    raise AssertionError(f"workflow step not found in {job_name}: {name}")


def _workflow_steps_by_job() -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step) for job_name in _workflow()["jobs"] for step in _workflow_steps(job_name)
    ]


def _workflow_step_names(job_name: str) -> list[str]:
    return [step["name"] for step in _workflow_steps(job_name)]


def _is_action(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


def _unpinned_action_refs(text: str) -> list[str]:
    offenders: list[str] = []
    for action_ref in USES_RE.findall(text):
        if "@" not in action_ref:
            offenders.append(action_ref)
            continue
        _, ref = action_ref.rsplit("@", maxsplit=1)
        if not FULL_SHA_RE.fullmatch(ref):
            offenders.append(action_ref)
    return offenders


def _dep_name(requirement: str) -> str:
    return re.split(r"[<>=!~ ]", requirement, maxsplit=1)[0]


def test_project_metadata_declares_pypi_release_contract() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert project["name"] == DIST_NAME
    assert project["version"] == EXPECTED_VERSION
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert "License ::" not in "\n".join(project.get("classifiers", []))
    assert CONSOLE_SCRIPT in project.get("scripts", {})

    sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})
    for requirement, rev in INTERNAL_DEPS:
        assert requirement in project["dependencies"]
        pkg = _dep_name(requirement)
        if rev is None:
            assert pkg not in sources, f"{pkg} must install from PyPI, not a uv git source"
        else:
            assert sources[pkg]["rev"] == rev


def test_release_workflow_dispatch_concurrency_and_permissions() -> None:
    workflow = _workflow()

    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
    assert dispatch["version"]["type"] == "string"
    assert dispatch["index"]["options"] == ["testpypi", "pypi"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ inputs.index }}-${{ inputs.version }}",
        "cancel-in-progress": False,
    }

    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert "id-token" not in jobs["build"]["permissions"]
    assert "environment" not in jobs["build"]

    assert jobs["publish"]["needs"] == "build"
    assert jobs["publish"]["environment"] == "${{ inputs.index }}"
    assert jobs["publish"]["permissions"] == {"contents": "read", "id-token": "write"}

    assert jobs["smoke-published"]["needs"] == "publish"
    assert jobs["smoke-published"]["permissions"] == {"contents": "read"}
    assert "id-token" not in jobs["smoke-published"]["permissions"]
    assert "environment" not in jobs["smoke-published"]

    assert jobs["github-release"]["needs"] == "smoke-published"
    assert jobs["github-release"]["if"] == "inputs.index == 'pypi'"
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    assert "id-token" not in jobs["github-release"]["permissions"]
    assert "environment" not in jobs["github-release"]


def test_release_workflow_uses_latest_reviewed_action_shas() -> None:
    text = _workflow_text()
    offenders: list[str] = []
    unpinned = _unpinned_action_refs(text)
    assert not unpinned, "release workflow actions must be pinned to full SHAs:\n" + "\n".join(
        unpinned
    )

    refs = USES_RE.findall(text)
    assert refs, "release workflow must use pinned actions"
    for action_ref in refs:
        action, ref = action_ref.rsplit("@", maxsplit=1)
        expected = EXPECTED_ACTION_REFS.get(action)
        if expected is None:
            offenders.append(f"unreviewed action {action}")
        elif ref != expected:
            offenders.append(f"{action}@{ref} does not match reviewed SHA {expected}")

    assert not offenders, "GitHub Action pins are stale:\n" + "\n".join(offenders)


def test_action_ref_parser_catches_mutable_and_missing_refs() -> None:
    sha = "a" * 40
    workflow = f"""
      - uses: actions/checkout@{sha}
      - uses: astral-sh/setup-uv@v8
      - uses: actions/cache
"""

    assert _unpinned_action_refs(workflow) == ["astral-sh/setup-uv@v8", "actions/cache"]


def test_release_checkout_does_not_persist_credentials() -> None:
    checkouts = [
        step for _job_name, step in _workflow_steps_by_job() if _is_action(step, "actions/checkout")
    ]

    assert checkouts
    for checkout in checkouts:
        assert checkout["uses"] == f"actions/checkout@{EXPECTED_ACTION_REFS['actions/checkout']}"
        assert checkout["with"]["persist-credentials"] is False


def test_release_tooling_is_sourced_from_pinned_core_checkout() -> None:
    workflow = _workflow()

    assert "uses" not in workflow["jobs"]["publish"]
    assert workflow["jobs"]["publish"]["steps"]

    tool_checkouts = [
        step
        for _job, step in _workflow_steps_by_job()
        if _is_action(step, "actions/checkout")
        and step.get("with", {}).get("repository") == "alexisbeaulieu97/untaped"
    ]
    assert len(tool_checkouts) == 2
    for step in tool_checkouts:
        assert step["with"]["ref"] == CORE_RELEASE_TOOL_SHA
        assert step["with"]["path"] == ".release-tool"
        assert step["with"]["persist-credentials"] is False

    assert "scripts/release.py" not in _workflow_text()


def test_release_setup_uv_steps_pin_version_and_expected_cache_settings() -> None:
    setup_steps = [
        (job_name, step)
        for job_name, step in _workflow_steps_by_job()
        if _is_action(step, "astral-sh/setup-uv")
    ]

    assert setup_steps, "release workflow must contain astral-sh/setup-uv steps"
    version_offenders = [
        f"{job_name}:{step['name']}"
        for job_name, step in setup_steps
        if step["with"]["version"] != EXPECTED_UV_VERSION
    ]
    assert not version_offenders, (
        f"setup-uv steps must pin uv {EXPECTED_UV_VERSION}:\n" + "\n".join(version_offenders)
    )

    cache_expectations = {
        ("build", "Install uv"): True,
        ("smoke-published", "Install uv"): True,
    }
    by_job_and_name = {(job_name, step["name"]): step for job_name, step in setup_steps}
    for key, expected_cache in cache_expectations.items():
        assert by_job_and_name[key]["with"]["enable-cache"] is expected_cache


def test_release_workflow_keeps_anti_burn_guards() -> None:
    text = _workflow_text()
    build_steps = _workflow_step_names("build")

    production_guard = _step_block("Guard production publish")
    assert "if: inputs.index == 'pypi'" in production_guard
    assert "refs/heads/main" in production_guard
    assert "exit 1" in production_guard

    version_guard = _step_block("Verify release version")
    assert (
        'python3 .release-tool/.github/release/release.py verify-version "$RELEASE_VERSION"'
        in version_guard
    )

    unused_guard = _step_block("Verify production release target is unused")
    assert "if: inputs.index == 'pypi'" in unused_guard
    assert (
        'python3 .release-tool/.github/release/release.py verify-target-unused "$RELEASE_VERSION"'
        in unused_guard
    )
    assert build_steps.index("Verify release version") < build_steps.index("Sync project")
    assert build_steps.index("Verify production release target is unused") < build_steps.index(
        "Sync project"
    )

    step_pattern = r"(?ms)^      - name: .+?(?=^      - name: |^  [a-zA-Z0-9_-]+:|\Z)"
    run_blocks = "\n".join(
        match.group(0) for match in re.finditer(step_pattern, text) if "run:" in match.group(0)
    )
    assert "${{ inputs.version }}" not in run_blocks


def test_release_workflow_validates_build_and_tool_local_wheel_smoke() -> None:
    text = _workflow_text()

    assert "uv sync --frozen" in text
    assert "uv run pre-commit run --all-files --show-diff-on-failure" in text
    assert "uv run mypy" in text
    assert "uv run pytest" in text
    assert "uv build --no-sources" in text

    smoke = _step_block("Smoke local wheel")
    assert "uv venv" in smoke
    assert "uv pip install" in smoke
    assert "dist/*.whl" in smoke
    assert "smoke-console" in smoke
    assert f"--package {DIST_NAME}" in smoke
    assert f"--console-script {CONSOLE_SCRIPT}" in smoke
    assert '--venv "$RUNNER_TEMP/local-wheel"' in smoke
    assert "untaped.api" not in smoke


def test_release_workflow_checks_internal_dependency_floors_on_selected_index() -> None:
    dependency_check = _step_block("Verify internal dependencies resolve from selected index")
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    for requirement, _rev in INTERNAL_DEPS:
        assert requirement in project["dependencies"]
        assert requirement not in dependency_check  # floors read from pyproject, never hardcoded

    assert (
        "python3 .release-tool/.github/release/release.py "
        "verify-internal-dependencies-published" in dependency_check
    )
    assert '--index "$RELEASE_INDEX"' in dependency_check
    assert "version may be burned" not in dependency_check.lower()


def test_release_workflow_hands_artifacts_to_trusted_publisher() -> None:
    text = _workflow_text()

    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in text
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in text
    assert "name: python-package-distributions" in text
    assert "path: dist/" in text
    assert "pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b" in text
    assert "repository-url: https://test.pypi.org/legacy/" in text
    assert "attestations: true" in text
    assert "uv publish" not in text


def test_release_workflow_smokes_published_tool_from_selected_index() -> None:
    install_uv = _workflow_step("smoke-published", "Install uv")
    smoke = _step_block("Smoke published package")

    assert install_uv["with"]["enable-cache"] is True
    assert "UV_INDEX=https://test.pypi.org/simple/" in smoke
    assert "UV_INDEX_STRATEGY=unsafe-best-match" in smoke
    assert 'uv venv --python 3.14 "$published_venv"' in smoke
    assert smoke.index('uv venv --python 3.14 "$published_venv"') < smoke.index("for attempt in")
    assert 'rm -rf "$published_venv"' not in smoke
    assert f"--refresh-package {DIST_NAME}" in smoke
    assert f"{DIST_NAME}==$RELEASE_VERSION" in smoke
    assert "smoke-console" in smoke
    assert f"--package {DIST_NAME}" in smoke
    assert f"--console-script {CONSOLE_SCRIPT}" in smoke
    assert '--venv "$published_venv"' in smoke
    assert "version may be burned" in smoke.lower()
    assert "bump patch" in smoke.lower()
    assert "untaped.api" not in smoke


def test_release_workflow_creates_github_release_only_after_pypi_smoke() -> None:
    text = _workflow_text()
    release = _step_block("Create GitHub release")

    assert "needs: smoke-published" in text
    assert "if: inputs.index == 'pypi'" in text
    assert "gh release create" in release
    assert ' --repo "$GITHUB_REPOSITORY"' in release
    assert "v$RELEASE_VERSION" in release
    assert f"{DIST_NAME} v$RELEASE_VERSION" in release


def test_docs_steer_users_to_pypi_install() -> None:
    if not PYPI_INSTALL_DOCS:
        return
    git_url = f"git+https://github.com/alexisbeaulieu97/{DIST_NAME}.git"
    for rel in PYPI_INSTALL_DOCS:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert f"uv tool install {DIST_NAME}" in text
        assert git_url in text
