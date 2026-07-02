# AGENTS.md - `untaped-ansible`

Single source of truth for this standalone CLI repo. If you change
architecture, command behavior, settings behavior, or the development
workflow, update this file in the same commit.

## Mission

`untaped-ansible` is a standalone CLI built on the `untaped` SDK. It owns the
`untaped-ansible` command tree for Ansible role/playbook dependency graphing,
reverse-impact analysis, and local dependency cache data. `untaped-github`
owns GitHub API access (its public client API is consumed here for source
refreshes); the `untaped` SDK provides config loading, output helpers,
HTTP/TLS primitives, profile selection, and shared errors.

## Hard Rules

1. **Keep `AGENTS.md` and the packaged skill up to date.** Architecture
   changes, new command patterns, settings changes, and major graph/source
   workflow changes must be documented here and in
   `src/untaped_ansible/skills/untaped-ansible/SKILL.md`.
2. **Prefer `uv` commands over manual dependency edits.** Use `uv add` and
   `uv add --group dev` when resolution permits; hand-edit tool config only.
3. **Expose the CLI through the `untaped-ansible` console script.**
   `untaped-ansible = "untaped_ansible.__main__:main"` in `[project.scripts]`
   is the public entry point. `main()` hands the Cyclopts `app` and a
   `ToolSpec(command="untaped-ansible", section="ansible",
   profile_model=AnsibleSettings, state_model=AnsibleState, skills=...)` to the
   SDK's `run_tool`, which mounts the shared `config` / `profile` / `skills`
   command groups and runs under the SDK error contract. The package
   `__init__.py` re-exports `app` lazily (PEP 562 `__getattr__`) so importing
   `untaped_ansible` never drags the whole CLI tree onto the import path before
   it is needed.
4. **Import the SDK from `untaped.api`.** Core helpers (`create_app`,
   `report_errors`, `render_rows`, `get_config_section`, `app_context`,
   errors, options, …) come from `untaped.api`, never from core internals. The
   tool-owned state helpers (`mutate_tool_state` / `read_tool_state`) also come
   from `untaped.api`; `untaped.testing` stays test-only. Command bodies read typed settings with
   `get_config_section("ansible", AnsibleSettings)` for the tool's own section
   and `get_config_section("github", GithubSettings)` for the foreign GitHub
   section — `get_config_section` builds the one-off section model directly, so
   the CLI app can be exercised in tests without going through `run_tool`, and
   the unregistered `github` section still resolves under the shared config's
   `extra="ignore"` contract. Use `app_context()` only for `ctx.http` /
   `ctx.ui(...)`. Profile selection is owned by the built-in `--profile`
   option, which works in any token position; commands declare no command-local
   `--profile`. Helpers like `cli/_refresh.py`'s `refresh_source` receive
   resolved GitHub/HTTP settings as arguments instead of reading ambient
   config.
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
11. **GitHub behavior belongs in `untaped-github`.** If this tool needs a
    missing GitHub operation, add an intentional public API there and test it.
    Do not reach into private internals or duplicate GitHub clients here.
12. **Finish with verification.** Run `uv run ruff check --fix`,
    `uv run ruff format`, `uv run mypy`, and `uv run pytest`.

## Architecture

```text
src/untaped_ansible/
├── __init__.py           # lazy PEP 562 re-export of app
├── __main__.py           # console-script entry point: run_tool(app, SPEC)
├── settings.py           # tool-owned profile + state models
├── _concurrency.py       # bounded_map thread-pool helper shared by application and infrastructure
├── cli/                  # Cyclopts composition root plus concern-specific commands
├── application/          # use cases and ports
├── domain/               # pure models, parser, graph, renderers
└── infrastructure/       # SQLite cache, local filesystem/git adapters
```

`__main__.py`'s `ToolSpec` declares the profile settings (`AnsibleSettings`),
the disjoint tool-managed state (`AnsibleState`, holding `sources` and
`aliases`), and the packaged `untaped-ansible` agent skill. Keep that static
skill asset current with major graph/source workflow changes.

Tests invoke the Cyclopts app directly. Command code reads its own `ansible`
section and the foreign `github` section through `get_config_section`, which
builds a one-off model for an unregistered section, so direct `app` invocations
need no plugin/section registration; `tests/conftest.py` only clears the
settings cache around every test. Entry-point/profile-state tests
(`tests/unit/test_tool_entrypoint.py`) drive the wired app via
`build_tool_app(app, SPEC)`.

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
`untaped.render_rows`. For `--format table`, row collections honor the
per-profile `ui:` settings and SDK built-in themes. For `--format json`,
`yaml`, and `raw`, row collections bypass configured themes with a plain
`UiContext()` so missing or invalid configured themes cannot break structured
or pipe-oriented output.

The first key in each raw row remains load-bearing: `alias list --format raw`
must emit aliases first, and source row commands must emit source names first
unless the user explicitly passes `--columns`. Keep those contracts stable for
shell pipelines.

`--format pipe` is supported on every row-style command. It emits the core
typed-pipe NDJSON envelope (one `{"untaped": "1", "kind": "<kind>", "record":
{...}}` line per row) so downstream consumers can route records by `kind`
without sniffing fields. Each producer tags its envelope with a namespaced
`kind` hint:

- `alias list` → `ansible.alias`
- `source list` and `source show` → `ansible.source`
- `source status` → `ansible.source-status`

Any new row-style producer that calls `render_rows` must pass a matching
`kind="ansible.<entity>"` (lowercase, kebab-case for multiword entities).

`graph` output is not a UI collection. Tree, Mermaid, and JSON graph output
are domain renderers over the graph model and must not be routed through the
row-style renderer. Consequently `graph` is NOT `--format pipe`-compatible: it
bypasses `render_rows`, so it carries no typed-pipe envelope or `kind` tag.

## Domain Contracts

- V1 graph nodes are Ansible playbook/project roots and roles only. Collections
  encountered in requirements files are reported as ignored, not traversed.
- Canonical dependency identity is GitHub `owner/repo`.
- Local role names and Galaxy names map to canonical repos through explicit
  aliases. Unknown declarations stay in the graph as unresolved nodes.
- The domain emits a graph model first; tree, Mermaid, and JSON are renderers
  over that model.
- `GraphEdge.id` is public JSON identity: `edge:` plus the first 16 hex
  characters of `sha256(relation + NUL + source_id + NUL + target_id)`. Do
  not change this format without treating it as a graph contract change. This
  is topological identity, not physical declaration identity: duplicate
  declarations with the same relation/source/target collapse to one
  representative graph edge.
- Graph cycles are detected after graph construction over the emitted,
  depth-bounded edge set. Cycle-closing edges must be emitted before traversal
  stops on a per-path cycle guard, otherwise cycle output hides the actual
  dependency relation. `DependencyGraph.cycles` records `kind`, `relation`,
  `node_ids`, and `edge_ids` separately for `requires` and `impacts`, including
  self-loops. `kind="cycle"` stores a closed ordered `node_ids` path and the
  corresponding path `edge_ids`. `kind="scc_group"` stores a sorted open SCC
  node set and sorted internal SCC edge IDs when elementary cycle enumeration
  exceeds the deterministic cap. Never emit a nondeterministic partial cycle
  list for a dense component.
- Tree output renders nested traversal paths for downstream and upstream
  independently. Each direction starts with the target node for that direction;
  when the requested target omits `--ref`, tree output should preserve concrete
  `target@ref` roots from indexed or live data instead of collapsing everything
  under the repo-only target. Do not flatten both directions into shared
  buckets; a node that appears in both directions should be visible in both
  sections.
- Tree rendering must keep its path-set guard as the loop breaker and
  user-facing `(cycle)` marker. Structured cycles are a graph-model contract
  for JSON and optional Mermaid comments; they are not a substitute for the
  tree renderer's recursion guard.
- Tree output is the human report. It sorts refs with branches before tags,
  promotes the repo's exact cached default branch first, sorts remaining
  branches naturally, sorts semver tags newest-first, and leaves refs without
  branch/tag metadata last. Mermaid and JSON keep graph model order.
- Parser reports distinguish empty YAML documents, valid mapping/list
  dependency documents, unsupported top-level shapes, and YAML parse errors.
  Empty, whitespace-only, and `---`-only files remain warning-free. Malformed
  or templated YAML and recognized dependency files with unsupported top-level
  shape return empty dependencies plus `ParseWarning`; they are visibility
  warnings, not repo failures. Recognized nested sections (`dependencies`,
  `roles`, `collections`) are warning-free when missing, null, or empty lists;
  present non-list values warn and are skipped.

## Cached Source Data

The SQLite cache and bare Git repository cache are tool-owned state. SQLite
stores named and fingerprinted source scans, repo/ref scan metadata, per-source
repo metadata such as exact default branch, resolved SHAs, graph edges,
unresolved declarations, resumable source-refresh progress, and timestamps.
SHA is authoritative. Branch and tag
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
configured by `ansible.index_path` and re-run `untaped-ansible source
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
a source or cached data. When a source is configured, source-backed graphing is
cache-first: it reads only completed SQLite source data unless `--refresh` is
passed. `--cached` is accepted as an explicit cache-only mode, while `--live`
is the explicit opt-in for live GitHub downstream reads. Upstream impact
requires a saved or inline source with refreshed data; any source-backed graph
selection whose completed baseline is missing fails early with the exact
refresh command instead of rendering partial upstream output. `graph --refresh`
is the explicit source-refresh request and the only graph path that touches
GitHub source inventory/probe APIs.
Cached downstream traversal is strict about refs. If a dependency points at
`repo@v1` and only `repo@main` is cached, traversal must warn and stop there
instead of falling back to another cached ref.

Graph flag conflicts are parse-time errors: `--refresh`/`--cached`/`--live`
form one Cyclopts mutually-exclusive group and
`--upstream`/`--downstream`/`--both` another
(`Group(..., validator=validators.LimitedChoice())` attached via
`Parameter(group=...)`; `LimitedChoice()` is the typed equivalent of
cyclopts' untyped `MutuallyExclusive` alias), so conflicting flags exit 2
before the command body runs. The cross-flag rule "`--refresh` requires
`--source` or inline source boundary selectors" cannot be a group validator;
it is the first statement of the command body so it fails (exit 2) before any
settings or index construction. Only `--source`, `--org`, `--team`, and
`--repo` count as source boundaries for this rule; modifiers such as `--path`,
`--ref-kind`, `--ref-pattern`, and `--ref-scan-default` cannot create an
inline source by themselves.

`ansible.freshness_ttl` is deprecated. Graph no longer performs implicit
freshness probes, so the setting does not affect graph defaults. Keep accepting
it for existing profiles, but do not add new behavior that depends on it; users
must pass `--refresh` or run `untaped-ansible source refresh NAME` when they
want remote data checked.

Stale-data and missing-cached-ref graph warnings carry the exact fix command.
The application layer stays free of CLI strings: the CLI composes
`GraphRequest.refresh_hint` (`untaped-ansible source refresh NAME` for saved
sources; re-run with `--refresh` for inline sources) and `BuildGraph` appends
it to those warnings.

Saved sources are configured under `ansible.sources`. `graph --source NAME` is
repeatable; repeated saved sources are additive and graph reads union their
existing `source:NAME` caches without creating a synthetic persisted source.
Inline graph selectors (`--org`, `--team`, `--repo`, `--path`, `--ref-kind`,
`--ref-pattern`, `--ref-scan-default`) are cached under deterministic internal
source keys so repeated commands can reuse the same scan. Do not reintroduce
user-facing `scope`, `index`, or `--direction` workflow concepts.

Saved source edits are patch-style list mutations. `source edit NAME` adds,
removes, or clears source selector lists without requiring the user to restate
the full source. It prints a concise status summary to stderr only; stdout
remains data-only. Missing removals should fail loudly so typos do not silently
leave stale selectors behind.

Source refresh defaults to all branches and all tags through
`ansible.ref_scan_default: all`. Users who need the older lower-cost behavior
can set `ansible.ref_scan_default: default_branch`, or
`--ref-scan-default default_branch` on an individual saved or inline source,
which scans only each repo's default branch under `refs/heads/` using
`GithubClient.batch_default_branch_refs(...)`. `--ref-pattern` narrows source
refs across branches and tags unless paired with `--ref-kind`; any explicit
ref kind or pattern uses the all-ref GraphQL probe path. `--ref-kind tags`
without a pattern scans all tags.

Source refresh is GitHub-inventory-backed and local-git-backed for object
transport. Ref probing is backend-selectable through
`ansible.source_refresh_backend` and `--backend auto|graphql|git` on
`source refresh`; `graph --refresh` accepts the same override, while
`graph --backend` without `--refresh` is a usage error. `auto` is the default:
GraphQL is the happy path, with bounded Git `ls-remote` fallback only for
fallback-eligible per-repo GraphQL probe failures or primary GraphQL
rate-limit exhaustion. The selected backend is per-run, is not persisted on
sources, and is not part of the refresh fingerprint. A refresh runs three
phases:

1. **Expansion** resolves explicit repos, orgs, and teams into a deduped,
   sorted repo list through `untaped-github`'s public
   `ResolveRepositoryInventory` API. Explicit repos win over org/team
   listings. Expansion failures are fatal: an unknown org/team/repo is a
   source misconfiguration, not a per-repo failure.
2. **Freshness probe** runs in repo-level batches so large sources can pause
   and resume. `GithubRefProbe` wraps `GithubClient.batch_repo_refs(...)` for
   all-ref scans and `GithubClient.batch_default_branch_refs(...)` for
   default-branch-only scans. All-ref GraphQL probes use 50-repo chunks;
   default-branch GraphQL probes use 100-repo chunks. `GitRemoteRefProbe`
   uses `git ls-remote --symref` (Git 2.8+) with the same HTTPS auth-header
   injection/redaction behavior as fetches, the normal 60-second Git timeout,
   and `ansible.probe_concurrency`. `git` mode still uses GitHub REST
   inventory expansion and still needs GitHub credentials for private sources;
   only the ref probe transport changes. The probe supplies each repo's exact
   default branch (expansion metadata is the fallback) and, for GraphQL
   results, cumulative `rate_limit_cost`, the minimum GraphQL
   `rate_limit_remaining`, and `rate_limit_reset_at`; the CLI warns on stderr
   when remaining drops below `ansible.source_refresh_rate_limit_floor`
   (default 500). `BatchRepoRefsResult.failures` rows from `untaped-github`
   are transient per-repo probe failures and should be surfaced as
   `transient ref probe failed: ...`, not as global GraphQL access failures.
   `AutoRefProbe` falls back to Git only for structured per-repo
   `ProbeFailure.kind in {"transient", "chunk"}` and never for `missing` or
   `git` failures. On `GithubGraphqlError(kind="rate_limited")`, v1 falls
   back the whole active probe target set to Git instead of preserving partial
   GraphQL successes. Global `/graphql` failures classified as
   `secondary_rate_limited`, `auth`, `forbidden`, or `unknown` must propagate
   out of `GithubRefProbe`, `RefreshGitSourceIndex`, `refresh_source`, and
   `run_source_refresh`: they are not per-repo failures, and source-backed
   `graph` should exit once instead of rendering stale graph output. Known
   limitation: if GitHub returns `200 OK` with per-alias `FORBIDDEN` for every
   repo, v1 still reports those repos as missing/inaccessible rather than
   inferring a global SSO or token-scope failure.
   GraphQL and Git probes agree for branches, lightweight tags, annotated tags,
   and tags-of-tags. GraphQL peels annotated tags up to two levels; Git
   `ls-remote` exposes fully peeled `^{}` targets for deeper annotated-tag
   chains. Rare 3+ level chains can therefore churn if a refresh switches
   backend. Normalizing those deeper chains is future work, so do not claim
   full backend-invariant SHAs for every possible tag chain.
3. **Fetch/parse** keeps bare repositories under `ansible.repo_cache_path`,
   fetches only changed or missing refs (bounded by
   `ansible.git_fetch_concurrency` / `--concurrency`), reads dependency
   files with Git object plumbing, and commits each processed repo batch
   without updating the source-wide completed baseline until the expanded repo
   queue is exhausted. Parse warnings from dependency files are returned as
   `RefreshResult.skipped_files` and rendered on stderr by the CLI; they are
   not persisted to SQLite and must not reappear later as cached graph
   warnings. The source-refresh repo batch size comes from
   `ansible.source_refresh_repo_batch_size` (default 100).

Refresh is resilient to per-repo failures. Probe misses (missing or
inaccessible repos), unrecovered probe failures, and per-repo fetch/parse
errors (`GitCacheError`, `UntapedError`) are recorded as `RefreshResult.failures`
instead of aborting the run, and pruning is scoped to succeeded repos during
partial commits: failed and untouched repos keep their previously cached refs
and repo metadata. After the summary, `source refresh` echoes each
`failed <repo>: <reason>` to stderr and exits 1
(`refresh completed with N repo failure(s); successes were saved`). Transient
GraphQL ref probe failures in explicit `graphql` mode also print a hint that a
normal rerun is safe and unchanged repos skip Git fetch/dependency scan work.
When auto fallback activates, `RefreshResult.probe_fallbacks` records the repos
and reason, and the CLI prints a stderr warning with the fallback count. Large
rate-limit fallbacks can be much slower because Git probing runs one network
subprocess per repo. The graph refresh path instead prepends a graph warning
and proceeds with possibly stale data for failed repos.

Local graph parsing and live GitHub downstream parsing surface dependency-file
parse skips through `DependencyGraph.warnings`. Local graph warnings render as
`skipped PATH: REASON`; live graph warnings render as
`skipped REPO@REF PATH: REASON` when a ref is known. The `DependencyIndex` port
remains a dependency-edge reader only; do not widen it for parser warnings.
Local graph warnings are collected by `cli/graph_commands.py` before overlaying
local edges. Live graph warnings are accumulated on `GithubDependencyIndex`
while traversal lazily reads raw files and are merged after graph construction.

Large refreshes are resumable. SQLite stores `source_refresh_progress` rows
for successful repos in the active source fingerprint. If the GraphQL budget
drops below `ansible.source_refresh_rate_limit_floor` while repos remain,
successful repos are committed, untouched repos/refs are preserved, the
source-wide completed baseline timestamp is not updated, and the command exits
1 with a resume hint. Re-running the same source refresh skips successful
progress rows and retries failed or unprocessed repos. The intended resumed
failure contract is that prior success in the active fingerprint can mark the
source complete when the queue is exhausted, while per-repo failures still make
the CLI exit non-zero. Only a budget-stop-free exhausted queue updates
`source_runs`, prunes removed repos/refs, and clears refresh progress.

When every expanded repo fails, there is nothing trustworthy to mark complete:
cached data and `scanned_at` stay untouched and the run does not look fresh —
staleness remains visible in `source status`. An empty expansion (zero repos)
is a successful refresh, not a failure, and still completes (pruning
now-unselected repos).

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
uv run untaped-ansible --help
```

The test suite has duplicate test-file basenames across directories, so
pytest must run with `--import-mode=importlib` (already configured in
`pyproject.toml` `addopts`). Do not run tests with `-o addopts=""` — that
drops the flag and collection fails on colliding module names.
