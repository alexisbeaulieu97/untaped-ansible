# AGENTS.md - `untaped-ansible`

Single source of truth for this standalone plugin repo. If you change
architecture, command behavior, settings behavior, or the development
workflow, update this file in the same commit.

## Mission

`untaped-ansible` is an `untaped` plugin. It owns the `untaped ansible`
command group for Ansible role/playbook dependency graphing, reverse-impact
analysis, and local dependency cache data. `untaped-github` owns GitHub API
access; `untaped` core owns the binary, plugin discovery, config/profile
resolution, output helpers, HTTP/TLS primitives, and shared errors.

## Hard Rules

1. **Keep `AGENTS.md` up to date.** Architecture changes and new command
   patterns must be documented here.
2. **Prefer `uv` commands over manual dependency edits.** Use `uv add` and
   `uv add --group dev` when resolution permits; hand-edit tool config only.
3. **Expose the plugin through the `untaped.plugins` entry point.**
   `ansible = "untaped_ansible.plugin:plugin"` is the public integration
   point. The plugin object must expose `id = "ansible"`, literal
   `untaped_api_version = 3`, and `manifest()` returning a `PluginManifest`.
   `plugin.py` must not import the CLI tree: the manifest's
   `CliSpec(import_path="untaped_ansible.cli:app")` defers that import until
   the `ansible` command is dispatched, and `untaped_ansible/__init__.py`
   re-exports `app` lazily through a PEP 562 module `__getattr__` for the
   same reason.
4. **Import the plugin SDK from `untaped.api`.** Core helpers (`create_app`,
   `report_errors`, `render_rows`, `plugin_context`, errors, options, …)
   come from `untaped.api`, never from core internals. The only exception is
   `untaped.config_file`, which the config repositories use for plugin-owned
   state reads/writes; `untaped.testing` stays test-only. Command bodies
   resolve settings once at the composition root via `plugin_context(profile)`
   and read sections with `ctx.section("ansible", AnsibleSettings)`; helpers
   like `cli/_refresh.py`'s `refresh_source` receive resolved GitHub/HTTP
   settings as arguments instead of reading ambient config.
5. **Use the 4-layer DDD layout.** `cli -> application -> domain`, with
   `infrastructure -> domain`; `application` and `infrastructure` must not
   import each other at runtime.
6. **Declare ports in `application/ports.py`.** Use cases depend on the
   narrowest `Protocol`; concrete adapters satisfy ports structurally.
7. **Use absolute imports.** `from untaped_ansible...`, never relative imports.
8. **Every source module has a module docstring.** Re-export `__init__.py`
   files are exempt.
9. **Cyclopts command signatures are explicit.** Use
   `Annotated[..., Parameter(...)]` and name documented commands/options
   explicitly. Required inputs are required positional-only params
   (`Parameter(help=...)` before `/`); a missing value renders
   `error: ... requires an argument` (exit 2) automatically — never an
   optional default plus a manual help dance.
10. **stdout is data only.** Prompts, progress, and status messages go to
    stderr via `echo(..., err=True)`.
11. **GitHub behavior belongs in `untaped-github`.** If this plugin needs a
    missing GitHub operation, add an intentional public API there and test it.
    Do not reach into private internals or duplicate GitHub clients here.
12. **Finish with verification.** Run `uv run ruff check --fix`,
    `uv run ruff format`, `uv run mypy`, and `uv run pytest`.

## Architecture

```text
src/untaped_ansible/
├── __init__.py           # lazy PEP 562 re-export of app
├── plugin.py             # entry-point plugin object (manifest only, no CLI imports)
├── settings.py           # plugin-owned config/state model
├── _concurrency.py       # bounded_map thread-pool helper shared by application and infrastructure
├── cli/                  # Cyclopts composition root plus concern-specific commands
├── application/          # use cases and ports
├── domain/               # pure models, parser, graph, renderers
└── infrastructure/       # SQLite cache, local filesystem/git adapters
```

The plugin manifest declares profile settings, top-level state, the lazily
imported `ansible` Cyclopts command, and the packaged `untaped-ansible`
agent skill. Keep that static skill asset current with major graph/source
workflow changes.

Tests that invoke the Cyclopts app directly must run after the production
plugin registration; `tests/conftest.py` discovers and registers installed
plugins once so `plugin_context().section(...)` resolves registered config
sections, and clears the settings cache around every test.

`cli/commands.py` is the Ansible Cyclopts composition root only. Keep graph
execution in `cli/graph_commands.py`, source management in
`cli/source_commands.py`, and alias management in `cli/alias_commands.py`.
Source-refresh wiring shared by the `source refresh` and `graph` paths —
adapter construction (`refresh_source`), the progress-wrapped
`run_source_refresh` runner, summary/rate-limit stderr output, and the
`pluralize` helper — lives in `cli/_refresh.py`; command modules must use it
instead of reaching into each other's helpers.

## CLI Output Contracts

Alias and source commands that emit row-style collections render through
`untaped.render_rows`. For `--format table`, row collections honor the global
`ui:` settings and registered theme plugins. For `--format json`, `yaml`, and
`raw`, row collections bypass configured themes with a plain `UiContext()` so
missing or invalid global themes cannot break structured or pipe-oriented
output.

The first key in each raw row remains load-bearing: `alias list --format raw`
must emit aliases first, and source row commands must emit source names first
unless the user explicitly passes `--columns`. Keep those contracts stable for
shell pipelines.

`graph` output is not a UI collection. Tree, Mermaid, and JSON graph output
are domain renderers over the graph model and must not be routed through the
row-style renderer.

## Domain Contracts

- V1 graph nodes are Ansible playbook/project roots and roles only. Collections
  encountered in requirements files are reported as ignored, not traversed.
- Canonical dependency identity is GitHub `owner/repo`.
- Local role names and Galaxy names map to canonical repos through explicit
  aliases. Unknown declarations stay in the graph as unresolved nodes.
- The domain emits a graph model first; tree, Mermaid, and JSON are renderers
  over that model.
- Tree output renders nested traversal paths for downstream and upstream
  independently. Each direction starts with the target node for that direction;
  when the requested target omits `--ref`, tree output should preserve concrete
  `target@ref` roots from indexed or live data instead of collapsing everything
  under the repo-only target. Do not flatten both directions into shared
  buckets; a node that appears in both directions should be visible in both
  sections.
- Tree output is the human report. It sorts refs with branches before tags,
  promotes the repo's exact cached default branch first, sorts remaining
  branches naturally, sorts semver tags newest-first, and leaves refs without
  branch/tag metadata last. Mermaid and JSON keep graph model order.

## Cached Source Data

The SQLite cache and bare Git repository cache are plugin-owned state. SQLite
stores named and fingerprinted source scans, repo/ref scan metadata, per-source
repo metadata such as exact default branch, resolved SHAs, graph edges,
unresolved declarations, and timestamps. SHA is authoritative. Branch and tag
names are resolved during source refresh and cached with freshness metadata.
Ref scans point at dependency snapshots keyed by source repo, SHA, dependency
path fingerprint, and alias fingerprint so multiple refs at the same commit can
share one parsed edge set. `source_ref_scans` remains the authoritative
repo/ref identity table for graph reads and display metadata.

Keep SQLite adapter methods in `infrastructure/sqlite_index.py` focused on
transaction boundaries and query flow. Refresh commits stay one transaction
and write in batches: existing snapshot ids are pre-looked-up in chunks, new
snapshots use `insert ... on conflict ... returning id` (edges are inserted
only for newly created snapshots), and ref-scan replaces/touches run as single
`executemany` statements. Schema creation helpers live in
`infrastructure/sqlite_schema.py`, and row/datetime mapping lives in
`infrastructure/sqlite_rows.py`.

Cache schema compatibility is intentionally not preserved; there is no
migration code. `sqlite_schema.SCHEMA_VERSION` is enforced through SQLite's
`PRAGMA user_version`: a fresh empty database gets the tables and the current
version stamp, while any database whose `user_version` differs (including
pre-versioning databases with tables but `user_version = 0`) makes
`ensure_schema` raise an `UntapedError` telling the user to delete the file
configured by `ansible.index_path` and re-run `untaped ansible source
refresh`. Bump `SCHEMA_VERSION` in the same commit as any schema change.

Source-index payload DTOs such as `IndexedDependency`, `GitRef`, `RefScan`,
`RefScanTouch`, and `SourceRepoMetadata` live in `domain/payloads.py` because
they cross the application/infrastructure boundary. `application/ports.py`
should stay protocol-only, and the layering tests enforce that `application`
and `infrastructure` do not import each other at runtime.

Graph reads are level-batched. The `DependencyIndex` port exposes batch reads
(`dependencies_batch`, `dependents_batch`, `cached_ref_metadata_batch`)
alongside the single-key reads; every requested key must appear in the result
mapping — with an empty list/tuple when nothing is indexed — so callers can
cache negative results, and a `None` ref in a requested pair keeps the
single-read "all indexed refs of the repo" semantics. `BuildGraph` walks each
direction as an explicit per-level worklist with per-path cycle stacks,
bulk-loads each depth level's uncached frontier through the batch reads
(~one batched query per depth level instead of one point query per node), and
replays recorded emissions depth-first so node/edge/warning ordering stays
identical to the previous recursive traversal. The SQLite adapter drives the
batch queries with chunked `with requested(...) as (values ...)` CTEs; the
overlay and multi-source wrappers implement the batch reads by delegating to
the wrapped index's batch reads and applying the same overlay/fan-out
semantics as their single-key counterparts, while the live-GitHub index
serves `dependencies_batch` with intentionally per-pair live reads (per-repo
tree/content fetches don't batch) and delegates its other batch reads to the
wrapped index.

`graph` is the primary user command. Downstream dependency reads do not require
a source or cached data. When a source is configured, downstream graphing
checks selected remote refs and prefers the refreshed cache; `--cached` skips
that check and uses SQLite as-is, while `--live` is the explicit opt-in for live
GitHub downstream reads. Upstream impact requires a saved or inline source with
refreshed data: `both` degrades to downstream output with an actionable warning
when upstream data is unavailable, while `upstream` fails early with the same
guidance. `graph --refresh` still exists as an explicit refresh request, but
source-backed graphing refreshes by default unless `--cached` is passed.
Cached downstream traversal is strict about refs. If a dependency points at
`repo@v1` and only `repo@main` is cached, traversal must warn and stop there
instead of falling back to another cached ref.

Saved sources are configured under `ansible.sources`. `graph --source NAME` is
repeatable; repeated saved sources are additive and graph reads union their
existing `source:NAME` caches without creating a synthetic persisted source.
Inline graph selectors (`--org`, `--team`, `--repo`, `--path`, `--ref-kind`,
`--ref-pattern`) are cached under deterministic internal source keys so repeated
commands can reuse the same scan. Do not reintroduce user-facing `scope`,
`index`, or `--direction` workflow concepts.

Saved source edits are patch-style list mutations. `source edit NAME` adds,
removes, or clears source selector lists without requiring the user to restate
the full source. It prints a concise status summary to stderr only; stdout
remains data-only. Missing removals should fail loudly so typos do not silently
leave stale selectors behind.

Source refresh defaults to all branches and all tags through
`ansible.ref_scan_default: all`. Users who need the older lower-cost behavior
can set `ansible.ref_scan_default: default_branch`, which scans only each
repo's default branch under `refs/heads/`. `--ref-pattern` narrows source refs
across branches and tags unless paired with `--ref-kind`; `--ref-kind tags`
without a pattern scans all tags.

Source refresh is git-only for data transport. A refresh runs three phases:

1. **Expansion** resolves explicit repos, orgs, and teams into a deduped,
   sorted repo list through a thread pool bounded by
   `ansible.probe_concurrency`. Explicit repos win over org/team listings.
   Expansion failures are fatal: an unknown org/team/repo is a source
   misconfiguration, not a per-repo failure.
2. **Freshness probe** is GraphQL-only: one `RefProbe.probe()` call
   (`infrastructure/github_ref_probe.py` wrapping
   `GithubClient.batch_repo_refs`) covers every repo with the union of
   needed ref kinds, driving ~50-repo aliased chunks concurrently under
   `ansible.probe_concurrency`. The probe also supplies each repo's exact
   default branch (expansion metadata is the fallback) and the minimum
   GraphQL `rate_limit_remaining` across chunks; the CLI warns on stderr
   when that drops below 500. Do not reintroduce `git ls-remote` ref
   checks.
3. **Fetch/parse** keeps bare repositories under `ansible.repo_cache_path`,
   fetches only changed or missing refs (bounded by
   `ansible.git_fetch_concurrency` / `--concurrency`), reads dependency
   files with Git object plumbing, and commits SQLite ref scans per
   `(source_key, repo, ref_kind, ref_name)` in one atomic transaction.

Refresh is resilient to per-repo failures. Probe misses (missing or
inaccessible repos) and per-repo fetch/parse errors (`GitCacheError`,
`HttpError`, `UntapedError`) are recorded as `RefreshResult.failures`
instead of aborting the run, and pruning is scoped to succeeded repos: a
failed repo's previously cached refs and repo metadata must survive the
commit (`commit_source_ref_refresh(..., failed_repos=...)`). After the
summary, `source refresh` echoes each `failed <repo>: <reason>` to stderr
and exits 1 (`refresh completed with N repo failure(s); successes were
saved`); the graph refresh path instead prepends a graph warning and
proceeds with possibly stale data for the failed repos.

When every expanded repo fails, there is nothing trustworthy to commit: the
index commit is skipped entirely so cached data and `scanned_at` stay
untouched and the run does not look fresh — staleness remains visible in
`source status`. An empty expansion (zero repos) is a successful refresh,
not a failure, and still commits (pruning now-unselected repos).

The application layer stays UI-free: `RefreshGitSourceIndex` emits
`RefreshProgressEvent` payloads (phase `expanding`/`probing`/`fetching`
with done/total/changed counts) through an optional `on_progress`
callback, and the CLI renders them through `cli/_progress.py`
(`StderrProgress`): in-place `\r` updates on a TTY, throttled lines
(~2s or 10% steps) otherwise. Refresh status and progress belong on
stderr; stdout stays data-only.

Do not create working checkouts for source indexing. Do not reintroduce
`ansible.cache_backend`, `--cache-backend`, or a REST/API source-refresh
fallback.

Saving or editing a source clears cached source data only when the saved
definition changes. Re-saving or editing to an identical source must preserve
refreshed cache data.

## Development Workflow

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check --fix
uv run ruff format
uv run untaped ansible --help
```
