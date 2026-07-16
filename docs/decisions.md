# Architecture decisions

The canonical decision state lives in the repository's empty initial public decision-only
orchestration store; tasks are forbidden. Agents should begin with
`untaped-orchestration brief --format json` and use the CLI with revision guards for all
further reads and mutations. Agents never use `--force-current`.

The committed [decision view](../.untaped/orchestration/views/decisions.md) is generated,
human-readable output and never tool input. Validate with
`untaped-orchestration check --local`, `untaped-orchestration fmt --check --local`, and
`untaped-orchestration render --check`. Recover through `check` and `render`, not hand edits.
