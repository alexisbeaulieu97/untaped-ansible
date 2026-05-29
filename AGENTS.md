# AGENTS.md - `untaped-ansible`

Single source of truth for this standalone plugin repo. If you change
architecture, command behavior, settings behavior, or the development
workflow, update this file in the same commit.

## Mission

`untaped-ansible` is an `untaped` plugin. It owns the `untaped ansible`
command group for Ansible role/playbook dependency graphing, reverse-impact
analysis, and local dependency indexing. `untaped-github` owns GitHub API
access; `untaped` core owns the binary, plugin discovery, config/profile
resolution, output helpers, HTTP/TLS primitives, and shared errors.

## Hard Rules

1. **Keep `AGENTS.md` up to date.** Architecture changes and new command
   patterns must be documented here.
2. **Prefer `uv` commands over manual dependency edits.** Use `uv add` and
   `uv add --group dev` when resolution permits; hand-edit tool config only.
3. **Expose the plugin through the `untaped.plugins` entry point.**
   `ansible = "untaped_ansible.plugin:plugin"` is the public integration
   point.
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
├── cli/                  # Typer commands; composition root
├── application/          # use cases and ports
├── domain/               # pure models, parser, graph, renderers
└── infrastructure/       # SQLite index, local filesystem/git adapters
```

## Domain Contracts

- V1 graph nodes are Ansible playbook/project roots and roles only. Collections
  encountered in requirements files are reported as ignored, not traversed.
- Canonical dependency identity is GitHub `owner/repo`.
- Local role names and Galaxy names map to canonical repos through explicit
  aliases. Unknown declarations stay in the graph as unresolved nodes.
- The domain emits a graph model first; tree, Mermaid, and JSON are renderers
  over that model.

## Indexing

The SQLite index is plugin-owned state. It stores named scan scopes, repo/ref
scan metadata, resolved SHAs, dependency files, graph edges, unresolved
declarations, and timestamps. SHA is authoritative. Branch and tag names are
resolved during index refresh and cached with freshness metadata.

`graph` warns on stale indexes and does not silently refresh unless the user
explicitly requests it.

## Development Workflow

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check --fix
uv run ruff format
uv run untaped ansible --help
```
