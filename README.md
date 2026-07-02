# untaped-ansible

`untaped-ansible` is a standalone Ansible dependency graph CLI built on the
[`untaped`](https://github.com/alexisbeaulieu97/untaped) SDK. It provides
role/playbook dependency graphs, upstream impact analysis, and Git-backed
source caches, plus the shared `config`, `profile`, and `skills` command
groups every untaped tool ships.

## Install

```bash
uv tool install untaped-ansible
```

`untaped-ansible` reads GitHub credentials from the shared `github` config
section (provided by `untaped-github`, which it depends on for the public
GitHub client API used by source refreshes):

```bash
untaped-ansible config set github.token ghp_xxx
```

`untaped-ansible` also ships the `untaped-ansible` agent skill, installed
via its `skills` command group:

```bash
untaped-ansible skills install
```

## Commands

```text
untaped-ansible graph TARGET --downstream
untaped-ansible graph TARGET --org acme --team platform --upstream --refresh
untaped-ansible graph TARGET --source platform --upstream --cached
untaped-ansible graph TARGET --source platform --source ops --upstream --cached
untaped-ansible graph TARGET --source platform --upstream
untaped-ansible graph TARGET --source platform --downstream --live
untaped-ansible graph TARGET --source platform --both --refresh --concurrency 12
untaped-ansible graph TARGET --source platform --both --refresh --backend git
untaped-ansible graph TARGET --source platform --both --format mermaid --output deps.mmd
untaped-ansible source save platform --org acme --team platform
untaped-ansible source refresh platform --concurrency 12
untaped-ansible source refresh platform --backend graphql
untaped-ansible source status platform
untaped-ansible alias add common acme/common
untaped-ansible config|profile|skills ...
```

`graph` uses `tree` output by default and also supports `mermaid` and
`json`. Local targets infer `owner/repo` from the checkout's GitHub
remote, with `--target-repo owner/name` available as an override. `--ref`
selects the branch, tag, or SHA used for live dependency reads and cached
upstream lookups.

Graph JSON includes stable edge IDs and detected cycles. Each edge carries
`id: "edge:<digest>"`, where the digest is the first 16 hex characters of
`sha256(relation + NUL + source_id + NUL + target_id)`. That ID is topological:
duplicate physical declarations for the same relation/source/target collapse
to one representative graph edge. `cycles` contains records with `kind`,
`relation`, `node_ids`, and `edge_ids`, detected separately for downstream
`requires` and upstream `impacts` edges. `kind: "cycle"` records use a closed
ordered `node_ids` path and matching path edge IDs; `kind: "scc_group"` records
use a sorted open node set and sorted internal SCC edge IDs when a component
has too many elementary cycles to enumerate deterministically. Cycle detection
runs only on the graph that was emitted, so `--depth` can hide a longer cycle
whose closing edge is outside the traversal horizon. Mermaid output emits
every edge in the graph and includes cycle/group comments when cycles are
known. Tree output keeps rendering cycles as path-local `(cycle)` markers.

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
selectors are present, graph is cache-first: it reads only completed SQLite
source data unless `--refresh` is passed. `--cached` is still accepted as an
explicit cache-only mode, and `--live` opts downstream traversal back into
live GitHub reads. `ansible.freshness_ttl` is deprecated and no longer affects
graph defaults; use `--refresh` or `untaped-ansible source refresh NAME` when
remote data should be checked.

Upstream graphs are source-backed because GitHub impact analysis needs a
search boundary. Repeat `--source` to union multiple saved source caches in one
graph. If a selected source has no completed baseline, graph fails with the
exact refresh command instead of rendering partial upstream output. Use
inline selectors for one-off work and pass `--refresh` when the inline cache
needs to be created or updated:

```bash
untaped-ansible graph acme/base --org acme --team platform --upstream --refresh
```

Git-backed source indexing is the default. The first source-backed run creates
bare repositories under `ansible.repo_cache_path`
(`~/.untaped/ansible-repositories` by default), fetches only the selected refs,
and reads dependency files from Git objects without checking out worktrees.
Later runs fetch/check the same ref set and reparse only refs whose SHA, tag
target, or dependency path configuration changed. Source-backed graphing uses
that completed cache by default; run `untaped-ansible source refresh NAME` or
pass graph `--refresh` when you want remote checks.

A refresh runs in repo-level batches. Org, team, and repo expansion is
resolved through the public `untaped-github` inventory API, with explicit repo
metadata winning over org/team listings. Ref probing is GraphQL-primary by
default (`ansible.source_refresh_backend: auto`): GraphQL handles the happy
path and Git `ls-remote` is used only for recoverable per-repo GraphQL probe
failures or primary GraphQL rate-limit exhaustion. Set
`ansible.source_refresh_backend` or pass `--backend auto|graphql|git` to
`source refresh`; `graph --refresh` accepts the same `--backend` override, and
`graph --backend` without `--refresh` is a usage error. The `git` backend still
uses GitHub REST inventory expansion and still needs GitHub credentials for
private sources; it only replaces the ref probe transport.

GraphQL probe results carry GraphQL cost, remaining budget, and reset
metadata. All-ref GraphQL chunks use `50` repos, while default-branch-only
GraphQL chunks use `100` repos. Git `ls-remote` probing runs one subprocess
per repo, uses `git ls-remote --symref` (Git 2.8+), the same HTTPS auth-header
redaction path as fetches, the normal 60-second Git timeout, and
`ansible.probe_concurrency` for concurrency. Large Git fallback runs can be
much slower than GraphQL batches; when fallback activates, a warning with the
repo count and reason is printed to stderr. Git fetches still only repos whose
selected refs changed, with per-repo concurrency bounded by
`ansible.git_fetch_concurrency` (default `8`, accepts `1..32`). Refresh
batches use `ansible.source_refresh_repo_batch_size` (default `100`).
`untaped-ansible graph --refresh` and `untaped-ansible source refresh` both
accept `--concurrency N` as a per-run override for fetch/parse work.
Refresh progress is reported on stderr with repo/ref counts, changed and
unchanged refs, edge count, elapsed time, and the Git concurrency used.

Dependency files that cannot be parsed are skipped with an explicit warning
instead of failing the whole repo. Empty files, whitespace-only files, and
`---`-only YAML documents stay warning-free. Missing, null, and empty
`dependencies`, `roles`, and `collections` sections also stay warning-free;
present non-list values warn and are skipped. Malformed YAML, templated YAML
that is not valid YAML, or recognized dependency files with an unsupported
shape produce `warning: skipped PATH: REASON` in local graph output and
`warning: skipped REPO@REF PATH: REASON` in live graph output when a ref is
known. During `source refresh`, the same condition is printed to stderr with
the same repo/ref form when a ref is present; skipped files are included in the
refresh result for that run but are not persisted into SQLite, so later cached
graph output does not invent past parse warnings.

In `auto` mode, primary GraphQL rate-limit exhaustion falls back to Git
`ls-remote` for the whole active probe target set. Secondary rate limiting,
auth failures, request-level forbidden responses, and unknown global GraphQL
access failures still abort immediately with one `error: ...` message. They
are not expanded into per-repo failures, and source-backed `graph` exits
instead of rendering stale cached output. A known limitation remains: if
GitHub returns `200 OK` with a per-repo `FORBIDDEN` error for every alias, that
still reports as per-repo missing/inaccessible rather than a global SSO or
token-scope failure.

GraphQL and Git probes record the same SHA for branches, lightweight tags,
annotated tags, and tags-of-tags. GraphQL currently peels annotated tags up to
two levels; Git `ls-remote` exposes fully peeled `^{}` targets for deeper
annotated-tag chains. Rare 3+ level tag chains can therefore look changed if a
run switches between GraphQL and Git fallback. Normalizing those deeper chains
is future work.

Per-repo failures do not abort a refresh: successful repos are committed,
each failure is listed on stderr, and `source refresh` exits 1. When every
repo fails, nothing is committed and the index is left untouched.
Transient GraphQL ref-probe failures in explicit `graphql` mode print an
additional hint to rerun the same `source refresh` command; unchanged repos
skip Git fetch and dependency scan work on the rerun. In `auto` mode, transient
GraphQL per-repo failures first fall back to Git `ls-remote`; only unrecovered
repos remain in the failure list.

If the GraphQL budget drops below `ansible.source_refresh_rate_limit_floor`
(default `500`) during a large refresh, successful repos are committed,
untouched repos and refs are preserved, the source-wide completed timestamp is
not updated, and the command exits 1 with a resume hint. Re-running the same
source refresh skips previously successful repos and retries failed or
unprocessed repos. If a resumed queue is exhausted after prior success, the
source can be marked complete even when the last batch has per-repo failures;
the CLI still exits 1 and lists those failures. Removed repos/refs are pruned
only after the expanded repo queue is exhausted without a budget stop.

Source refreshes scan all branches and all tags by default
(`ansible.ref_scan_default: all`). Set
`ansible.ref_scan_default: default_branch` when runtime matters more than broad
upstream coverage, or set `--ref-scan-default default_branch` on an individual
saved or inline source. The default-branch strategy uses a cheaper GitHub
query that does not request the `refs(...)` connection. `--ref-pattern` narrows source refs, so
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
untaped-ansible source save platform --org acme --team platform
untaped-ansible source refresh platform
untaped-ansible graph acme/base --source platform --upstream
```

Use `ansible.git_clone_protocol: ssh` when normal SSH keys are preferred over
HTTPS token auth. HTTPS mode passes the configured GitHub token to Git as a
transient auth header and does not store it in cached remotes.

The SQLite index enforces a schema version through `PRAGMA user_version`.
There are no migrations: when an existing index was created by a different
tool version, commands fail with an error naming the exact
`ansible.index_path` file to delete and the
`untaped-ansible source refresh NAME` command to run afterwards.

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
uv run untaped-ansible --help
```

See [AGENTS.md](./AGENTS.md) for architecture rules and dependency graph
contracts.

## Security

Please report suspected vulnerabilities privately. See
[SECURITY.md](./SECURITY.md).

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) and [AGENTS.md](./AGENTS.md) for the
local workflow, architecture rules, and dependency graph contracts.

## License

MIT. See [LICENSE](./LICENSE).
