---
name: untaped-ansible
description: Use the untaped-ansible CLI.
---

# Untaped Ansible

Use this skill when the user wants an agent to operate `untaped-ansible` for Ansible dependency graphing and impact analysis.

## Setup

- The tool command is `untaped-ansible`.
- Settings live under `profiles.<name>.ansible` and top-level `ansible` state.
- `untaped-ansible` analyzes Ansible project roots and roles. Collections in requirements files are reported as ignored rather than traversed.
- GitHub API access belongs to `untaped-github`; do not duplicate GitHub client behavior inside Ansible workflows.

## Command Patterns

- `untaped-ansible graph` is the main command for downstream, upstream, and combined dependency views.
- Saved sources are selected with repeatable `--source NAME`; repeated sources are additive.
- Inline selectors such as `--org`, `--team`, `--repo`, `--path`, `--ref-kind`, and `--ref-pattern` are also additive where accepted.
- `--cached` uses SQLite cache as-is; source-backed graphing refreshes by default unless cached mode is selected.
- `--live` is the explicit opt-in for live GitHub downstream reads.
- `--refresh`, `--cached`, and `--live` are mutually exclusive, as are `--upstream`, `--downstream`, and `--both`; conflicting flags are usage errors (exit 2). `--refresh` also requires `--source` or inline selectors.
- `--team` accepts ORG/SLUG; a bare SLUG is allowed when exactly one `--org` is given and normalizes to ORG/SLUG.
- Inline source selectors are cached under a deterministic fingerprint key, so repeating the identical graph command reuses the scan.
- `ansible.freshness_ttl` (seconds, opt-in; default unset = always check) lets graph skip the remote freshness check per source selection whose last scan is within the TTL, with one stderr info line per skipped selection. `--refresh` always probes; `--cached` always skips all checks.
- Row-style commands (`alias list`, `source list`, `source show`, `source status`) accept `--format pipe` for typed NDJSON: `untaped-ansible source list --format pipe` emits one `{"untaped":"1","kind":"ansible.source","record":{...}}` line per row (kinds: `ansible.alias`, `ansible.source`, `ansible.source-status`). `graph` does not support `--format pipe`.

## Agent Guidance

- Prefer JSON for machine reasoning, tree output for human impact reports, and Mermaid only when the user wants a diagram.
- Do not collapse refs. A dependency at `repo@v1` is distinct from `repo@main`.
- Upstream impact requires refreshed source data; if unavailable, prompt the user to refresh or configure sources.
- `source refresh` is resilient to per-repo failures: successes are saved, each `failed <repo>: <reason>` is listed on stderr, and the command exits 1 with `refresh completed with N repo failure(s); successes were saved`. Treat that exit as partial success, not a hard failure. If every repo failed, nothing is committed: the index (including its freshness timestamp) is left unchanged and the command exits 1 with `refresh failed for all N repo(s); index left unchanged`. Graph refreshes warn instead and proceed with possibly stale data for failed repos.
- Refresh progress and status print to stderr only; stdout stays machine-readable.
- The SQLite index enforces a schema version; there are no migrations. On a version mismatch, commands fail with an actionable error naming the exact index file to delete and the `untaped-ansible source refresh NAME` command to run afterwards.
