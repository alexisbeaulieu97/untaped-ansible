# untaped-ansible

`untaped-ansible` is the Ansible dependency graph plugin for
[`untaped`](https://github.com/alexisbeaulieu97/untaped). It adds the
`untaped ansible` command group for role/playbook dependency graphs,
upstream impact analysis, and GitHub-backed source caches.

## Install

Install `untaped`, `untaped-github`, and this plugin from git. This
plugin requires `untaped-github>=0.2.0`, which provides the public
GitHub client API used by source refreshes.

```bash
uv tool install "git+https://github.com/alexisbeaulieu97/untaped.git" \
  --with "untaped-github @ git+https://github.com/alexisbeaulieu97/untaped-github.git" \
  --with "untaped-ansible @ git+https://github.com/alexisbeaulieu97/untaped-ansible.git" \
  --no-sources \
  --force
```

Configure GitHub auth through `untaped-github`:

```bash
untaped config set github.token ghp_xxx
```

## Commands

```text
untaped ansible graph TARGET --downstream
untaped ansible graph TARGET --org acme --team platform --upstream --refresh
untaped ansible graph TARGET --source platform --upstream
untaped ansible graph TARGET --source platform --downstream --live
untaped ansible graph TARGET --source platform --both --format mermaid --output deps.mmd
untaped ansible source save platform --org acme --team platform
untaped ansible source refresh platform
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

Downstream graphs do not require a source or cached data. Local targets are read
from disk. GitHub URL and `owner/repo` targets read declared dependencies live
from GitHub when no source is configured. When `--source` or inline source
selectors are present, downstream graphing prefers the refreshed cache; pass
`--live` to opt back into live GitHub reads for downstream traversal.

Upstream graphs are source-backed because GitHub impact analysis needs a
search boundary. Use inline selectors for one-off work:

```bash
untaped ansible graph acme/base --org acme --team platform --upstream --refresh
```

`--refresh` is explicit and required before scanning GitHub. Source refreshes
scan only each repo's default branch by default. Use `--ref-pattern '*'` to scan
all selected branches, and add `--ref-kind tags` only when tags are needed. More
specific patterns such as `--ref-pattern 'release/*'` are sent to GitHub as
narrow matching-ref prefixes before local `fnmatch` filtering. The same inline
selector set is cached under a deterministic fingerprint, so later identical
commands can reuse the refreshed source data without `--refresh`.

Save a reusable source for repeated work or CI:

```bash
untaped ansible source save platform --org acme --team platform
untaped ansible source refresh platform
untaped ansible graph acme/base --source platform --upstream
```

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
