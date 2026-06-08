"""Contract tests for the CI GitHub Actions workflow."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s#]+)(?:\s+#.*)?\s*$", re.MULTILINE)
EXPECTED_UV_VERSION = "0.11.19"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step_blocks(text: str) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("      - "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(block) for block in blocks]


def _with_value(step: str, field: str) -> str | None:
    with_indent: int | None = None
    for line in step.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        if with_indent is None:
            if re.fullmatch(r"with:\s*(?:#.*)?", stripped):
                with_indent = indent
            continue
        if indent <= with_indent:
            break

        match = re.fullmatch(rf"\s*{re.escape(field)}:\s*(.+?)(?:\s+#.*)?", line)
        if match:
            return match.group(1).strip().strip("\"'")

    return None


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


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    offenders = _unpinned_action_refs(_workflow_text())

    assert not offenders, "GitHub Action refs must be pinned to full SHAs:\n" + "\n".join(offenders)


def test_action_ref_parser_handles_inline_comments() -> None:
    sha = "a" * 40
    workflow = f"""
      - uses: actions/checkout@{sha} # pinned
      - uses: astral-sh/setup-uv@v6 # mutable
      - uses: actions/cache # missing ref
"""

    assert _unpinned_action_refs(workflow) == ["astral-sh/setup-uv@v6", "actions/cache"]


def test_checkout_steps_do_not_persist_credentials() -> None:
    checkout_steps = [
        step for step in _step_blocks(_workflow_text()) if "uses: actions/checkout@" in step
    ]

    assert checkout_steps, "workflow must contain an actions/checkout step"
    offenders = [
        step.splitlines()[0].strip()
        for step in checkout_steps
        if _with_value(step, "persist-credentials") != "false"
    ]

    assert not offenders, "checkout steps must set persist-credentials: false"


def test_setup_uv_steps_pin_uv_version_and_enable_cache() -> None:
    setup_steps = [
        step for step in _step_blocks(_workflow_text()) if "uses: astral-sh/setup-uv@" in step
    ]

    assert setup_steps, "workflow must contain an astral-sh/setup-uv step"
    version_offenders = [
        step.splitlines()[0].strip()
        for step in setup_steps
        if _with_value(step, "version") != EXPECTED_UV_VERSION
    ]
    cache_offenders = [
        step.splitlines()[0].strip()
        for step in setup_steps
        if _with_value(step, "enable-cache") != "true"
    ]

    assert not version_offenders, f"setup-uv steps must pin uv {EXPECTED_UV_VERSION}"
    assert not cache_offenders, "setup-uv steps must set enable-cache: true"
