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
   `untaped_api_version = 1`, and `register(registry)`.
4. **Use the 4-layer DDD layout.** `cli -> application -> domain`, with
   `infrastructure -> domain`; `application` and `infrastructure` must not
   import each other at runtime.
5. **Declare ports in `application/ports.py`.** Use cases depend on the
   narrowest `Protocol`; concrete adapters satisfy ports structurally.
6. **Use absolute imports.** `from untaped_ansible...`, never relative imports.
7. **Every source module has a module docstring.** Re-export `__init__.py`
   files are exempt.
8. **Every Typer app and every command with required args sets
   `no_args_is_help=True`.**
9. **stdout is data only.** Prompts, progress, and status messages go to
   stderr via `typer.echo(..., err=True)`.
10. **GitHub behavior belongs in `untaped-github`.** If this plugin needs a
    missing GitHub operation, add an intentional public API there and test it.
    Do not reach into private internals or duplicate GitHub clients here.
11. **Finish with verification.** Run `uv run ruff check --fix`,
    `uv run ruff format`, `uv run mypy`, and `uv run pytest`.

## Architecture

```text
src/untaped_ansible/
├── __init__.py           # re-exports app
├── plugin.py             # entry-point plugin object
├── settings.py           # plugin-owned config/state model
├── cli/                  # Typer composition root plus concern-specific commands
├── application/          # use cases and ports
├── domain/               # pure models, parser, graph, renderers
└── infrastructure/       # SQLite cache, local filesystem/git adapters
```

The plugin object registers profile settings, top-level state, the
`ansible` Typer command, and the packaged `untaped-ansible` agent skill.
Keep that static skill asset current with major graph/source workflow
changes.

`cli/commands.py` is the Ansible Typer composition root only. Keep graph
execution in `cli/graph_commands.py`, source management and refresh wiring in
`cli/source_commands.py`, and alias management in `cli/alias_commands.py`.

## CLI Output Contracts

Alias and source commands that emit row-style collections render through
`cli/_rendering.py`. For `--format table`, row collections honor the global
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
transaction boundaries and query flow. Schema creation helpers live in
`infrastructure/sqlite_schema.py`, and row/datetime mapping lives in
`infrastructure/sqlite_rows.py`. The legacy source-cache cleanup is
cache-schema-breaking. Cache schema compatibility is intentionally not
preserved yet; users must delete the file configured by `ansible.index_path`
and refresh saved sources after schema-breaking changes.

Source-index payload DTOs such as `IndexedDependency`, `GitRef`, `RefScan`,
`RefScanTouch`, and `SourceRepoMetadata` live in `domain/payloads.py` because
they cross the application/infrastructure boundary. `application/ports.py`
should stay protocol-only, and the layering tests enforce that `application`
and `infrastructure` do not import each other at runtime.

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

Source refresh is git-only. Git refresh keeps bare repositories under
`ansible.repo_cache_path`, checks selected remote ref SHAs before touching the
local bare cache, fetches only changed or missing refs, reads dependency files
with Git object plumbing, and commits SQLite ref scans per `(source_key, repo,
ref_kind, ref_name)`. Do not create working checkouts for source indexing. Do
not reintroduce `ansible.cache_backend`, `--cache-backend`, or a REST/API
source-refresh fallback.
Git source refresh supports bounded per-repo concurrency through
`ansible.git_fetch_concurrency` and `--concurrency`; repo/team/org expansion
remains serial, and SQLite mutation remains a single atomic commit after repo
workers finish successfully. Refresh status and progress belong on stderr.

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
