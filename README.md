# untaped-ansible

`untaped-ansible` is the Ansible dependency graph plugin for
[`untaped`](https://github.com/alexisbeaulieu97/untaped). It adds the
`untaped ansible` command group for role/playbook dependency graphs,
upstream impact analysis, and Git-backed source caches.

## Install

Install `untaped`, `untaped-github`, and this plugin from git. This
plugin requires `untaped-github>=0.2.0`, which provides the public
GitHub client API used by source refreshes.

```bash
uv tool install "git+https://github.com/alexisbeaulieu97/untaped.git@v0.1.4" \
  --with "untaped-github @ git+https://github.com/alexisbeaulieu97/untaped-github.git@v0.2.0" \
  --with "untaped-ansible @ git+https://github.com/alexisbeaulieu97/untaped-ansible.git@v0.1.0" \
  --no-sources \
  --force
```

Configure GitHub auth through `untaped-github`:

```bash
untaped config set github.token ghp_xxx
```

This plugin also contributes the `untaped-ansible` agent skill. After the
plugin is installed, use the core
[`untaped` agent skill docs](https://github.com/alexisbeaulieu97/untaped/blob/main/docs/skills.md)
to install it for Codex or Claude.

## Commands

```text
untaped ansible graph TARGET --downstream
untaped ansible graph TARGET --org acme --team platform --upstream
untaped ansible graph TARGET --source platform --upstream --cached
untaped ansible graph TARGET --source platform --source ops --upstream --cached
untaped ansible graph TARGET --source platform --upstream
untaped ansible graph TARGET --source platform --downstream --live
untaped ansible graph TARGET --source platform --both --concurrency 12
untaped ansible graph TARGET --source platform --both --format mermaid --output deps.mmd
untaped ansible source save platform --org acme --team platform
untaped ansible source refresh platform --concurrency 12
untaped ansible source status platform
untaped ansible alias add common acme/common
```

`graph` uses `tree` output by default and also supports `mermaid` and
`json`. Local targets infer `owner/repo` from the checkout's GitHub
remote, with `--target-repo owner/name` available as an override. `--ref`
selects the branch, tag, or SHA used for live dependency reads and cached
upstream lookups.

Use relationship flags in user terms:

- `--downstream`: dependencies used by the target.
- `--upstream`: repos/roles/playbooks that depend on the target.
- `--both`: both directions; this is the default when no direction flag is
  passed.

`--upstream`, `--downstream`, and `--both` are mutually exclusive, as are
`--refresh`, `--cached`, and `--live`; combining flags from either group is
a usage error caught at parse time.

Tree output renders each direction as a nested traversal. Each populated
direction starts with the target node for that direction, then continues to its
children. When the graph target omits `--ref` and cached or live data contains
multiple matching refs, those refs are shown separately as `target@ref` roots.
A repo/ref that appears in both directions is rendered in both sections. In
tree output, refs are ordered for scanning: branches first with the repo's
default branch first, then tags in newest-first semantic-version order, then
refs without cached branch/tag metadata.

Downstream graphs do not require a source or cached data. Local targets are read
from disk. GitHub URL and `owner/repo` targets read declared dependencies live
from GitHub when no source is configured. When `--source` or inline source
selectors are present, graph checks the selected remote refs, updates changed
repo/ref entries in SQLite, then reads from the cache; pass `--cached` to skip
the remote freshness check, or `--live` to opt back into live GitHub reads for
downstream traversal. Set `ansible.freshness_ttl` (seconds; unset by default,
and `0` means unset) to skip that pre-graph check for sources refreshed within
the TTL; `--refresh` always forces a refresh and `--cached` always skips the
check regardless of the TTL.

Upstream graphs are source-backed because GitHub impact analysis needs a
search boundary. Repeat `--source` to union multiple saved source caches in one
graph. Each saved source is refreshed under its own cache key unless `--cached`
is passed. Use inline selectors for one-off work:

```bash
untaped ansible graph acme/base --org acme --team platform --upstream
```

Git-backed source indexing is the default. The first source-backed run creates
bare repositories under `ansible.repo_cache_path`
(`~/.untaped/ansible-repositories` by default), fetches only the selected refs,
and reads dependency files from Git objects without checking out worktrees.
Later runs fetch/check the same ref set and reparse only refs whose SHA, tag
target, or dependency path configuration changed. Use `--cached` for the
offline/fast path when you do not want remote checks.

A refresh runs in three phases. Org, team, and repo expansion runs in
parallel, bounded by `ansible.probe_concurrency` (default `8`, accepts
`1..32`). One batched GraphQL probe then checks every repo's refs — there is
no per-repo `git ls-remote`. Finally, Git fetches only the repos whose refs
changed, with per-repo concurrency bounded by `ansible.git_fetch_concurrency`
(default `8`, accepts `1..32`); `ansible graph` and `ansible source refresh`
both accept `--concurrency N` as a per-run override. Refresh progress is
reported on stderr with repo/ref counts, changed and unchanged refs, edge count,
elapsed time, and the Git concurrency used.

Per-repo failures do not abort a refresh: successful repos are committed,
each failure is listed on stderr, and `source refresh` exits 1. When every
repo fails, nothing is committed and the index is left untouched.

Source refreshes scan all branches and all tags by default
(`ansible.ref_scan_default: all`). Set
`ansible.ref_scan_default: default_branch` when runtime matters more than broad
upstream coverage. `--ref-pattern` narrows source refs, so
`--ref-pattern v3` scans matching branches and tags unless `--ref-kind` is also
provided. `--ref-kind tags` without a pattern scans all tags. Patterns such as
`--ref-pattern 'release/*'` filter the probed refs client-side; refs that
changed are then fetched with exact Git refspecs. The same inline selector set
is cached under a deterministic fingerprint, so later identical commands reuse
the same SQLite source key.

Source-backed downstream traversal is strict about refs. If the graph needs
`repo@v1` and the source cache only has `repo@main`, graph stops at that node
and warns instead of silently falling back. Available-ref warnings use the same
branch/tag ordering as tree output. Scan the matching branch/tag, use `--cached`
only when the existing SQLite index is known to be complete, or pass `--live`
when you explicitly want downstream dependencies read from GitHub.

Save a reusable source for repeated work or CI:

```bash
untaped ansible source save platform --org acme --team platform
untaped ansible source refresh platform
untaped ansible graph acme/base --source platform --upstream
```

Use `ansible.git_clone_protocol: ssh` when normal SSH keys are preferred over
HTTPS token auth. HTTPS mode passes the configured GitHub token to Git as a
transient auth header and does not store it in cached remotes.

The SQLite index enforces a schema version through `PRAGMA user_version`.
There are no migrations: when an existing index was created by a different
plugin version, commands fail with an error naming the exact
`ansible.index_path` file to delete and the
`untaped ansible source refresh NAME` command to run afterwards.

`source status NAME` reports whether a configured source has never been
refreshed, is stale, or is fresh. Unknown source names return an error so
typos do not look like missing cache data.

When a source has exactly one `--org`, `--team` accepts a bare team slug
and stores it as `org/slug`. Use `--team org/slug` when a source has no
org or multiple orgs.

## Development

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check --fix
uv run ruff format
uv run untaped ansible --help
```

See [AGENTS.md](./AGENTS.md) for architecture rules and dependency graph
contracts.
