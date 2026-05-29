# untaped-ansible

`untaped-ansible` is the Ansible dependency graph plugin for
[`untaped`](https://github.com/alexisbeaulieu97/untaped). It adds the
`untaped ansible` command group for role/playbook dependency trees,
reverse-impact analysis, and GitHub-backed dependency indexing.

## Install

Install `untaped`, `untaped-github`, and this plugin from git. This
plugin requires `untaped-github>=0.2.0`, which provides the public
GitHub client API used by `index refresh`.

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
untaped ansible graph TARGET
untaped ansible graph TARGET --format mermaid
untaped ansible index refresh --scope prod
untaped ansible index status --scope prod
untaped ansible scope add prod --org acme
untaped ansible alias add common acme/common
```

`graph` uses `tree` output by default and also supports `mermaid` and
`json`. Local targets infer `owner/repo` from the checkout's GitHub
remote, with `--repo owner/name` available as an override.
GitHub URL and `owner/repo` targets read declared dependencies live from
GitHub for `deps` graphs; reverse impact still comes from a refreshed
named scope index.

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
