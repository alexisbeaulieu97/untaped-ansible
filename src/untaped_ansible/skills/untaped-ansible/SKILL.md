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
- Inline selectors such as `--org`, `--team`, `--repo`, `--path`, `--ref-kind`, `--ref-pattern`, and `--ref-scan-default` are also additive where accepted.
- Source-backed graphing is cache-first: it reads completed SQLite source data by default and touches GitHub only when `--refresh` is passed or `source refresh` is run. If no completed baseline exists, graph fails with the exact refresh command instead of rendering partial upstream output.
- Source refresh ref probing is backend-selectable. The default is `ansible.source_refresh_backend: auto`; use `source refresh NAME --backend auto|graphql|git` or `graph --refresh --backend auto|graphql|git` for a per-run override. `graph --backend ...` without `--refresh` is a usage error. Backend choice is not persisted on sources.
- `--cached` uses SQLite cache as-is.
- `--live` is the explicit opt-in for live GitHub downstream reads.
- `--refresh`, `--cached`, and `--live` are mutually exclusive, as are `--upstream`, `--downstream`, and `--both`; conflicting flags are usage errors (exit 2). `--refresh` also requires `--source` or inline source boundary selectors (`--org`, `--team`, or `--repo`); modifiers such as `--path`, `--ref-kind`, `--ref-pattern`, and `--ref-scan-default` do not count by themselves.
- `--team` accepts ORG/SLUG; a bare SLUG is allowed when exactly one `--org` is given and normalizes to ORG/SLUG.
- Inline source selectors are cached under a deterministic fingerprint key, so repeating the identical graph command reuses the scan.
- `ansible.freshness_ttl` is deprecated and no longer affects graph defaults; use `--refresh` or `source refresh NAME` when remote data should be checked.
- Row-style commands (`alias list`, `source list`, `source show`, `source status`) accept `--format pipe` for typed NDJSON: `untaped-ansible source list --format pipe` emits one `{"untaped":"1","kind":"ansible.source","record":{...}}` line per row (kinds: `ansible.alias`, `ansible.source`, `ansible.source-status`). `graph` does not support `--format pipe`.
- `source show` is a single entity: under `--format table` it renders a vertical key:value detail view, and under `--format json` it emits a bare object (`{…}`, not a one-element `[{…}]`). The collection commands (`source list`/`status`, `alias list`) still render tables and JSON arrays.

## Agent Guidance

- Prefer JSON for machine reasoning, tree output for human impact reports, and Mermaid only when the user wants a diagram.
- Do not collapse refs. A dependency at `repo@v1` is distinct from `repo@main`.
- Upstream impact requires refreshed source data; if unavailable, prompt the user to refresh or configure sources.
- In `auto` mode, primary GraphQL rate-limit exhaustion falls back to Git `ls-remote` for the whole active probe target set. Secondary rate limiting, auth, request-level forbidden, and unknown global GitHub GraphQL access failures still abort `source refresh` and source-backed `graph` once through the SDK error path. Do not treat these as per-repo failures or proceed with stale graph output. Known limitation: all-repo per-alias `FORBIDDEN` in a `200 OK` response still reports as per-repo missing/inaccessible.
- `source refresh` expands orgs/teams/repos through the public `untaped-github` inventory API. All-ref scans use `batch_repo_refs` with 50-repo GraphQL chunks; per-source/global `default_branch` scans with no explicit ref filters use `batch_default_branch_refs` with 100-repo GraphQL chunks. The `git` backend still uses GitHub REST inventory and still needs credentials for private sources; it only replaces the ref probe transport. Git probing uses `git ls-remote --symref` (Git 2.8+), the normal 60-second Git timeout, `ansible.probe_concurrency`, and the same HTTPS auth-header redaction path as fetches.
- `source refresh` is resilient to per-repo failures: successes are saved, each `failed <repo>: <reason>` is listed on stderr, and the command exits 1 with `refresh completed with N repo failure(s); successes were saved`. Treat that exit as partial success, not a hard failure. If every repo failed, the completed baseline is left unchanged and the command exits 1 with `refresh failed for all N repo(s); index left unchanged`. In explicit `graphql` mode, transient GraphQL ref-probe failures from `BatchRepoRefsResult.failures` print an extra safe-rerun hint because unchanged repos skip Git fetch/dependency scan work. In `auto`, transient GraphQL per-repo failures and residual chunk failures first fall back to Git `ls-remote`; unrecovered repos remain failures. Graph refreshes warn instead and proceed with possibly stale data for failed repos.
- When auto fallback activates, the CLI prints a stderr warning with the repo count and reason. Large primary-rate-limit fallbacks can be much slower because Git probing runs one network subprocess per repo.
- Large `source refresh` runs are resumable. If the GraphQL budget drops below `ansible.source_refresh_rate_limit_floor` (default `500`) while repos remain, successful repo batches are committed, untouched repos/refs are preserved, the source-wide completed timestamp is not updated, and the command exits 1 with a resume hint. Re-run the same `source refresh` command to skip successful repos and retry failed or unprocessed repos. Prior success in the active fingerprint can complete a resumed source when the queue is exhausted, but per-repo failures still make the CLI exit non-zero.
- Tune large refreshes with `ansible.source_refresh_repo_batch_size` (default `100`) and `ansible.source_refresh_rate_limit_floor` (default `500`).
- Refresh progress and status print to stderr only; stdout stays machine-readable.
- The SQLite index enforces a schema version; there are no migrations. On a version mismatch, commands fail with an actionable error naming the exact index file to delete and the `untaped-ansible source refresh NAME` command to run afterwards.
