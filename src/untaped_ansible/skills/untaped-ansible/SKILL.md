---
name: untaped-ansible
description: Use the untaped Ansible plugin.
---

# Untaped Ansible

Use this skill when the user wants an agent to operate `untaped ansible` for Ansible dependency graphing and impact analysis.

## Setup

- The plugin command group is `untaped ansible`.
- Settings live under `profiles.<name>.ansible` and top-level `ansible` state.
- The plugin analyzes Ansible project roots and roles. Collections in requirements files are reported as ignored rather than traversed.
- GitHub API access belongs to `untaped-github`; do not duplicate GitHub client behavior inside Ansible workflows.

## Command Patterns

- `untaped ansible graph` is the main command for downstream, upstream, and combined dependency views.
- Saved sources are selected with repeatable `--source NAME`; repeated sources are additive.
- Inline selectors such as `--org`, `--team`, `--repo`, `--path`, `--ref-kind`, and `--ref-pattern` are also additive where accepted.
- `--cached` uses SQLite cache as-is; source-backed graphing refreshes by default unless cached mode is selected.
- `--live` is the explicit opt-in for live GitHub downstream reads.

## Agent Guidance

- Prefer JSON for machine reasoning, tree output for human impact reports, and Mermaid only when the user wants a diagram.
- Do not collapse refs. A dependency at `repo@v1` is distinct from `repo@main`.
- Upstream impact requires refreshed source data; if unavailable, prompt the user to refresh or configure sources.
- `source refresh` is resilient to per-repo failures: successes are saved, each `failed <repo>: <reason>` is listed on stderr, and the command exits 1 with `refresh completed with N repo failure(s); successes were saved`. Treat that exit as partial success, not a hard failure. Graph refreshes warn instead and proceed with possibly stale data for failed repos.
- Refresh progress and status print to stderr only; stdout stays machine-readable.
- Cache schema compatibility is not preserved yet. The legacy source-cache cleanup is cache-schema-breaking; users must delete `ansible.index_path` and refresh saved sources after schema-breaking errors.
