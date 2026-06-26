# Contributing

Thanks for contributing to `untaped-ansible`.

## Local Setup

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check --fix
uv run ruff format
uv run untaped-ansible --help
uv run pre-commit run --all-files
```

## Documentation

Update `README.md`, `AGENTS.md`, and
`src/untaped_ansible/skills/untaped-ansible/SKILL.md` when a change affects
command behavior, settings, workflows, output contracts, or agent-facing
usage.

## Sensitive Data

Do not include secrets, real GitHub tokens, real customer configurations,
private repositories, production logs, health exports, or private data in
issues, tests, fixtures, or examples. Use synthetic data for tests and
examples.
