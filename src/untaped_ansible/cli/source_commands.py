"""Source management commands and refresh helpers for the Ansible plugin."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from cyclopts import Parameter, validators
from untaped.api import (
    ColumnsOption,
    FormatOption,
    UntapedError,
    create_app,
    echo,
    get_config_section,
    plugin_context,
    render_rows,
    report_errors,
)
from untaped_github import GithubSettings

from untaped_ansible.application.refresh_index import RefreshResult
from untaped_ansible.cli._refresh import pluralize, run_source_refresh
from untaped_ansible.infrastructure import (
    AliasRepository,
    SourceRepository,
    SqliteDependencyIndex,
)
from untaped_ansible.settings import AnsibleSettings, SourceDefinition, normalize_team_refs

_FINGERPRINT_HEX_CHARS = 16

ConcurrencyOption = Annotated[
    int | None,
    Parameter(
        name="--concurrency",
        validator=validators.Number(gte=1, lte=32),
        help="Git-backed source refresh concurrency; defaults to ansible.git_fetch_concurrency.",
    ),
]

app = create_app(
    name="source",
    help="Manage reusable GitHub sources.",
)


@app.command(name="save")
def source_save_command(
    name: Annotated[str, Parameter(help="Source name.")],
    *,
    orgs: Annotated[
        list[str] | None,
        Parameter(name="--org", help="GitHub org to scan.", consume_multiple=False),
    ] = None,
    teams: Annotated[
        list[str] | None,
        Parameter(
            name="--team",
            help=(
                "GitHub team as ORG/SLUG; a bare SLUG is allowed when exactly one "
                "--org is given and normalizes to ORG/SLUG."
            ),
            consume_multiple=False,
        ),
    ] = None,
    repos: Annotated[
        list[str] | None,
        Parameter(name="--repo", help="GitHub repo as owner/name.", consume_multiple=False),
    ] = None,
    paths: Annotated[
        list[str] | None,
        Parameter(name="--path", help="Dependency file path.", consume_multiple=False),
    ] = None,
    ref_kinds: Annotated[
        list[str] | None,
        Parameter(
            name="--ref-kind",
            help="Ref namespace to scan: heads or tags; omit to use ansible.ref_scan_default.",
            consume_multiple=False,
        ),
    ] = None,
    ref_patterns: Annotated[
        list[str] | None,
        Parameter(
            name="--ref-pattern",
            help="fnmatch pattern for branch/tag names; omit to use ansible.ref_scan_default.",
            consume_multiple=False,
        ),
    ] = None,
) -> None:
    """Save a reusable GitHub source."""
    with report_errors():
        source = _source_definition(
            name=name,
            orgs=orgs,
            teams=teams,
            repos=repos,
            paths=paths,
            ref_kinds=ref_kinds,
            ref_patterns=ref_patterns,
        )
        source_repo = SourceRepository()
        previous = source_repo.get(name)
        source_repo.upsert(source)
        settings = get_config_section("ansible", AnsibleSettings)
        if previous != source:
            SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        echo(f"saved source {name!r}", err=True)


@app.command(name="edit")
def source_edit_command(
    name: Annotated[str, Parameter(help="Source name.")],
    *,
    add_orgs: Annotated[
        list[str] | None,
        Parameter(name="--add-org", consume_multiple=False),
    ] = None,
    remove_orgs: Annotated[
        list[str] | None,
        Parameter(name="--remove-org", consume_multiple=False),
    ] = None,
    clear_orgs: Annotated[bool, Parameter(name="--clear-org", negative="")] = False,
    add_teams: Annotated[
        list[str] | None,
        Parameter(name="--add-team", consume_multiple=False),
    ] = None,
    remove_teams: Annotated[
        list[str] | None,
        Parameter(name="--remove-team", consume_multiple=False),
    ] = None,
    clear_teams: Annotated[bool, Parameter(name="--clear-team", negative="")] = False,
    add_repos: Annotated[
        list[str] | None,
        Parameter(name="--add-repo", consume_multiple=False),
    ] = None,
    remove_repos: Annotated[
        list[str] | None,
        Parameter(name="--remove-repo", consume_multiple=False),
    ] = None,
    clear_repos: Annotated[bool, Parameter(name="--clear-repo", negative="")] = False,
    add_paths: Annotated[
        list[str] | None,
        Parameter(name="--add-path", consume_multiple=False),
    ] = None,
    remove_paths: Annotated[
        list[str] | None,
        Parameter(name="--remove-path", consume_multiple=False),
    ] = None,
    clear_paths: Annotated[bool, Parameter(name="--clear-path")] = False,
    add_ref_kinds: Annotated[
        list[str] | None,
        Parameter(name="--add-ref-kind", consume_multiple=False),
    ] = None,
    remove_ref_kinds: Annotated[
        list[str] | None,
        Parameter(name="--remove-ref-kind", consume_multiple=False),
    ] = None,
    clear_ref_kinds: Annotated[bool, Parameter(name="--clear-ref-kind")] = False,
    add_ref_patterns: Annotated[
        list[str] | None,
        Parameter(name="--add-ref-pattern", consume_multiple=False),
    ] = None,
    remove_ref_patterns: Annotated[
        list[str] | None,
        Parameter(name="--remove-ref-pattern", consume_multiple=False),
    ] = None,
    clear_ref_patterns: Annotated[bool, Parameter(name="--clear-ref-pattern")] = False,
) -> None:
    """Patch a saved GitHub source definition."""
    with report_errors():
        source_repo = SourceRepository()
        previous = source_repo.get(name)
        if previous is None:
            raise UntapedError(f"unknown source: {name!r}")
        edited, changes = _edit_source_definition(
            previous,
            add_orgs=add_orgs,
            remove_orgs=remove_orgs,
            clear_orgs=clear_orgs,
            add_teams=add_teams,
            remove_teams=remove_teams,
            clear_teams=clear_teams,
            add_repos=add_repos,
            remove_repos=remove_repos,
            clear_repos=clear_repos,
            add_paths=add_paths,
            remove_paths=remove_paths,
            clear_paths=clear_paths,
            add_ref_kinds=add_ref_kinds,
            remove_ref_kinds=remove_ref_kinds,
            clear_ref_kinds=clear_ref_kinds,
            add_ref_patterns=add_ref_patterns,
            remove_ref_patterns=remove_ref_patterns,
            clear_ref_patterns=clear_ref_patterns,
        )
        if previous == edited:
            echo(f"source {name!r} unchanged", err=True)
            return
        source_repo.upsert(edited)
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        echo(f"updated source {name!r}: {', '.join(changes)}", err=True)


@app.command(name="list")
def source_list_command(*, fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List saved sources."""
    with report_errors():
        rows = [_source_row(source) for source in SourceRepository().entries()]
        rendered = render_rows(
            rows,
            fmt=fmt,
            columns=columns,
            kind="ansible.source",
            empty="No sources configured. Add one with `untaped-ansible source save <name>`.",
        )
        if rendered:
            echo(rendered)


@app.command(name="show")
def source_show_command(
    name: Annotated[str, Parameter(help="Source name.")],
    *,
    fmt: FormatOption = "table",
    columns: ColumnsOption = None,
) -> None:
    """Show one saved source."""
    with report_errors():
        source = SourceRepository().get(name)
        if source is None:
            raise UntapedError(f"unknown source: {name!r}")
        echo(render_rows([_source_row(source)], fmt=fmt, columns=columns, kind="ansible.source"))


@app.command(name="remove")
def source_remove_command(name: Annotated[str, Parameter(help="Source name.")]) -> None:
    """Remove a saved source."""
    with report_errors():
        removed = SourceRepository().remove(name)
        if not removed:
            raise UntapedError(f"unknown source: {name!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        echo(f"removed source {name!r}", err=True)


@app.command(name="status")
def source_status_command(
    name: Annotated[str | None, Parameter(help="Source to inspect.")] = None,
    *,
    fmt: FormatOption = "table",
    columns: ColumnsOption = None,
) -> None:
    """Show cached source data status."""
    with report_errors():
        settings = get_config_section("ansible", AnsibleSettings)
        index = SqliteDependencyIndex(settings.index_path)
        source_repo = SourceRepository()
        configured = {source.name: source for source in source_repo.entries()}
        if name is not None and name not in configured:
            raise UntapedError(f"unknown source: {name!r}")
        names = [name] if name is not None else sorted(configured)
        rows = [
            _source_status_row(
                source_name,
                index=index,
                configured_sources=configured,
                stale_after=settings.stale_after,
            )
            for source_name in names
        ]
        rendered = render_rows(
            rows,
            fmt=fmt,
            columns=columns,
            kind="ansible.source-status",
            empty="No sources scanned yet. Run `untaped-ansible source refresh <name>`.",
        )
        if rendered:
            echo(rendered)


@app.command(name="refresh")
def source_refresh_command(
    name: Annotated[str, Parameter(help="Source name.")],
    *,
    concurrency: ConcurrencyOption = None,
) -> None:
    """Refresh a saved source from GitHub."""
    with report_errors():
        ctx = plugin_context()
        source = SourceRepository().get(name)
        if source is None:
            raise UntapedError(f"unknown source: {name!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        git_concurrency = concurrency or settings.git_fetch_concurrency
        result = run_source_refresh(
            source,
            source_key=_saved_source_key(name),
            action="refreshed",
            label=f"source {name!r}",
            index=SqliteDependencyIndex(settings.index_path),
            aliases=aliases,
            settings=settings,
            github_settings=get_config_section("github", GithubSettings),
            http=ctx.http,
            concurrency=git_concurrency,
            ui=ctx.ui(strict=False),
        )
        if result.failures:
            for failure in result.failures:
                echo(f"failed {failure.repo}: {failure.reason}", err=True)
            raise UntapedError(_refresh_failure_message(result))


def _refresh_failure_message(result: RefreshResult) -> str:
    count = len(result.failures)
    if count == result.repos:
        return f"refresh failed for all {pluralize(count, 'repo')}; index left unchanged"
    return f"refresh completed with {pluralize(count, 'repo failure')}; successes were saved"


def _source_definition(
    *,
    name: str,
    orgs: list[str] | None,
    teams: list[str] | None,
    repos: list[str] | None,
    paths: list[str] | None,
    ref_kinds: list[str] | None,
    ref_patterns: list[str] | None,
) -> SourceDefinition:
    try:
        return SourceDefinition(
            name=name,
            orgs=orgs or [],
            teams=teams or [],
            repos=repos or [],
            dependency_paths=paths or [],
            ref_kinds=ref_kinds or [],
            ref_patterns=ref_patterns or [],
        )
    except ValueError as exc:
        raise UntapedError(str(exc)) from exc


def _edit_source_definition(
    source: SourceDefinition,
    *,
    add_orgs: list[str] | None,
    remove_orgs: list[str] | None,
    clear_orgs: bool,
    add_teams: list[str] | None,
    remove_teams: list[str] | None,
    clear_teams: bool,
    add_repos: list[str] | None,
    remove_repos: list[str] | None,
    clear_repos: bool,
    add_paths: list[str] | None,
    remove_paths: list[str] | None,
    clear_paths: bool,
    add_ref_kinds: list[str] | None,
    remove_ref_kinds: list[str] | None,
    clear_ref_kinds: bool,
    add_ref_patterns: list[str] | None,
    remove_ref_patterns: list[str] | None,
    clear_ref_patterns: bool,
) -> tuple[SourceDefinition, list[str]]:
    if not _source_edit_requested(
        add_orgs,
        remove_orgs,
        clear_orgs,
        add_teams,
        remove_teams,
        clear_teams,
        add_repos,
        remove_repos,
        clear_repos,
        add_paths,
        remove_paths,
        clear_paths,
        add_ref_kinds,
        remove_ref_kinds,
        clear_ref_kinds,
        add_ref_patterns,
        remove_ref_patterns,
        clear_ref_patterns,
    ):
        raise UntapedError("source edit requires at least one mutation flag")

    changes: list[str] = []
    orgs = _apply_source_list_edit(
        source.name,
        "org",
        source.orgs,
        add=add_orgs,
        remove=remove_orgs,
        clear=clear_orgs,
        changes=changes,
    )
    teams = _apply_source_list_edit(
        source.name,
        "team",
        source.teams,
        add=_normalized_team_edit_values(add_teams, orgs),
        remove=_normalized_team_edit_values(remove_teams, source.orgs),
        clear=clear_teams,
        changes=changes,
    )
    repos = _apply_source_list_edit(
        source.name,
        "repo",
        source.repos,
        add=add_repos,
        remove=remove_repos,
        clear=clear_repos,
        changes=changes,
    )
    dependency_paths = _apply_source_list_edit(
        source.name,
        "path",
        source.dependency_paths,
        add=add_paths,
        remove=remove_paths,
        clear=clear_paths,
        changes=changes,
    )
    ref_kinds = _apply_source_list_edit(
        source.name,
        "ref-kind",
        source.ref_kinds,
        add=add_ref_kinds,
        remove=remove_ref_kinds,
        clear=clear_ref_kinds,
        changes=changes,
    )
    ref_patterns = _apply_source_list_edit(
        source.name,
        "ref-pattern",
        source.ref_patterns,
        add=add_ref_patterns,
        remove=remove_ref_patterns,
        clear=clear_ref_patterns,
        changes=changes,
    )
    try:
        edited = SourceDefinition(
            name=source.name,
            orgs=orgs,
            teams=teams,
            repos=repos,
            dependency_paths=dependency_paths,
            ref_kinds=ref_kinds,
            ref_patterns=ref_patterns,
        )
    except ValueError as exc:
        raise UntapedError(str(exc)) from exc
    return edited, changes


def _source_edit_requested(*groups: object) -> bool:
    for group in groups:
        if isinstance(group, bool):
            if group:
                return True
            continue
        if group:
            return True
    return False


def _apply_source_list_edit(
    source_name: str,
    label: str,
    current: list[str],
    *,
    add: list[str] | None,
    remove: list[str] | None,
    clear: bool,
    changes: list[str],
) -> list[str]:
    edited = list(current)
    if clear and edited:
        edited = []
        changes.append(f"cleared {label}")
    for value in remove or []:
        if value not in edited:
            raise UntapedError(f"source {source_name!r} has no {label} {value}")
        edited.remove(value)
        changes.append(f"removed {label} {value}")
    for value in add or []:
        if value in edited:
            continue
        edited.append(value)
        changes.append(f"added {label} {value}")
    return edited


def _normalized_team_edit_values(values: list[str] | None, orgs: list[str]) -> list[str] | None:
    if not values:
        return values
    try:
        return normalize_team_refs(values, orgs)
    except ValueError as exc:
        raise UntapedError(str(exc)) from exc


def _source_row(source: SourceDefinition) -> dict[str, object]:
    return source.model_dump()


def _saved_source_key(name: str) -> str:
    return f"source:{name}"


def _inline_source_key(source: SourceDefinition) -> str:
    payload = source.model_dump(exclude={"name"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"inline:{hashlib.sha256(encoded).hexdigest()[:_FINGERPRINT_HEX_CHARS]}"


def _source_status_row(
    name: str,
    *,
    index: SqliteDependencyIndex,
    configured_sources: dict[str, SourceDefinition],
    stale_after: int,
) -> dict[str, object]:
    key = _saved_source_key(name)
    status = index.status(key)
    configured = name in configured_sources
    if status is None:
        return {
            "source": name,
            "source_key": key,
            "state": "not-refreshed" if configured else "missing-source",
            "configured": configured,
            "scanned_at": None,
            "repos": 0,
            "refs": 0,
            "edges": 0,
            "stale": None,
        }
    stale = index.is_stale(key, max_age_seconds=stale_after)
    return {
        "source": name,
        **status.model_dump(),
        "state": "stale" if stale else "fresh",
        "configured": configured,
        "stale": stale,
    }
