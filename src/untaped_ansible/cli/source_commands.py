"""Source management commands and refresh helpers for the Ansible plugin."""

from __future__ import annotations

import hashlib
import json
import time
from base64 import b64encode

import typer
from untaped import (
    ColumnsOption,
    FormatOption,
    ProfileOverrideOption,
    UntapedError,
    get_config_section,
    get_core_settings,
    profile_override,
    report_errors,
)
from untaped_github import GithubClient, GithubSettings

from untaped_ansible.application.refresh_git_index import RefreshGitSourceIndex
from untaped_ansible.application.refresh_index import RefreshResult
from untaped_ansible.cli._rendering import render_rows
from untaped_ansible.infrastructure import (
    AliasRepository,
    GitRepositoryCache,
    SourceRepository,
    SqliteDependencyIndex,
)
from untaped_ansible.settings import AnsibleSettings, SourceDefinition, normalize_team_refs

_FINGERPRINT_HEX_CHARS = 16

app = typer.Typer(
    name="source",
    help="Manage reusable GitHub sources.",
    no_args_is_help=True,
)


@app.command("save", no_args_is_help=True)
def source_save_command(
    name: str,
    orgs: list[str] | None = typer.Option(None, "--org", help="GitHub org to scan."),
    teams: list[str] | None = typer.Option(
        None,
        "--team",
        help="GitHub team slug with one --org, or ORG/SLUG.",
    ),
    repos: list[str] | None = typer.Option(None, "--repo", help="GitHub repo as owner/name."),
    paths: list[str] | None = typer.Option(None, "--path", help="Dependency file path."),
    ref_kinds: list[str] | None = typer.Option(
        None,
        "--ref-kind",
        help="Ref namespace to scan: heads or tags; omit to use ansible.ref_scan_default.",
    ),
    ref_patterns: list[str] | None = typer.Option(
        None,
        "--ref-pattern",
        help="fnmatch pattern for branch/tag names; omit to use ansible.ref_scan_default.",
    ),
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
        typer.echo(f"saved source {name!r}", err=True)


@app.command("edit", no_args_is_help=True)
def source_edit_command(
    name: str,
    add_orgs: list[str] | None = typer.Option(None, "--add-org", help="Add a GitHub org."),
    remove_orgs: list[str] | None = typer.Option(
        None,
        "--remove-org",
        help="Remove a GitHub org.",
    ),
    clear_orgs: bool = typer.Option(False, "--clear-org", help="Remove all GitHub orgs."),
    add_teams: list[str] | None = typer.Option(
        None,
        "--add-team",
        help="Add a GitHub team slug with one org, or ORG/SLUG.",
    ),
    remove_teams: list[str] | None = typer.Option(
        None,
        "--remove-team",
        help="Remove a GitHub team slug with one org, or ORG/SLUG.",
    ),
    clear_teams: bool = typer.Option(False, "--clear-team", help="Remove all GitHub teams."),
    add_repos: list[str] | None = typer.Option(
        None,
        "--add-repo",
        help="Add a GitHub repo as owner/name.",
    ),
    remove_repos: list[str] | None = typer.Option(
        None,
        "--remove-repo",
        help="Remove a GitHub repo as owner/name.",
    ),
    clear_repos: bool = typer.Option(False, "--clear-repo", help="Remove all GitHub repos."),
    add_paths: list[str] | None = typer.Option(
        None,
        "--add-path",
        help="Add a dependency file path.",
    ),
    remove_paths: list[str] | None = typer.Option(
        None,
        "--remove-path",
        help="Remove a dependency file path.",
    ),
    clear_paths: bool = typer.Option(False, "--clear-path", help="Remove all dependency paths."),
    add_ref_kinds: list[str] | None = typer.Option(
        None,
        "--add-ref-kind",
        help="Add a ref namespace to scan: heads or tags.",
    ),
    remove_ref_kinds: list[str] | None = typer.Option(
        None,
        "--remove-ref-kind",
        help="Remove a ref namespace: heads or tags.",
    ),
    clear_ref_kinds: bool = typer.Option(
        False,
        "--clear-ref-kind",
        help="Remove all ref namespace filters.",
    ),
    add_ref_patterns: list[str] | None = typer.Option(
        None,
        "--add-ref-pattern",
        help="Add a branch/tag fnmatch pattern.",
    ),
    remove_ref_patterns: list[str] | None = typer.Option(
        None,
        "--remove-ref-pattern",
        help="Remove a branch/tag fnmatch pattern.",
    ),
    clear_ref_patterns: bool = typer.Option(
        False,
        "--clear-ref-pattern",
        help="Remove all branch/tag patterns.",
    ),
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
            typer.echo(f"source {name!r} unchanged", err=True)
            return
        source_repo.upsert(edited)
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        typer.echo(f"updated source {name!r}: {', '.join(changes)}", err=True)


@app.command("list")
def source_list_command(fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List saved sources."""
    with report_errors():
        rows = [_source_row(source) for source in SourceRepository().entries()]
        typer.echo(render_rows(rows, fmt=fmt, columns=columns))


@app.command("show", no_args_is_help=True)
def source_show_command(
    name: str,
    fmt: FormatOption = "table",
    columns: ColumnsOption = None,
) -> None:
    """Show one saved source."""
    with report_errors():
        source = SourceRepository().get(name)
        if source is None:
            raise UntapedError(f"unknown source: {name!r}")
        typer.echo(render_rows([_source_row(source)], fmt=fmt, columns=columns))


@app.command("remove", no_args_is_help=True)
def source_remove_command(name: str) -> None:
    """Remove a saved source."""
    with report_errors():
        removed = SourceRepository().remove(name)
        if not removed:
            raise UntapedError(f"unknown source: {name!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        typer.echo(f"removed source {name!r}", err=True)


@app.command("status")
def source_status_command(
    name: str | None = typer.Argument(None, help="Source to inspect."),
    fmt: FormatOption = "table",
    columns: ColumnsOption = None,
    profile: ProfileOverrideOption = None,
) -> None:
    """Show cached source data status."""
    with report_errors(), profile_override(profile):
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
        typer.echo(render_rows(rows, fmt=fmt, columns=columns))


@app.command("refresh", no_args_is_help=True)
def source_refresh_command(
    name: str,
    concurrency: int | None = typer.Option(
        None,
        "--concurrency",
        min=1,
        max=32,
        help="Git-backed source refresh concurrency; defaults to ansible.git_fetch_concurrency.",
    ),
    profile: ProfileOverrideOption = None,
) -> None:
    """Refresh a saved source from GitHub."""
    with report_errors(), profile_override(profile):
        source = SourceRepository().get(name)
        if source is None:
            raise UntapedError(f"unknown source: {name!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        git_concurrency = concurrency or settings.git_fetch_concurrency
        started_at = time.perf_counter()
        result = _refresh_source(
            source,
            source_key=_saved_source_key(name),
            index=SqliteDependencyIndex(settings.index_path),
            aliases=aliases,
            settings=settings,
            concurrency=git_concurrency,
        )
        typer.echo(
            _refresh_summary(
                "refreshed",
                f"source {name!r}",
                result,
                concurrency=git_concurrency,
                elapsed=time.perf_counter() - started_at,
            ),
            err=True,
        )


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


def _refresh_source(
    source: SourceDefinition,
    *,
    source_key: str,
    index: SqliteDependencyIndex,
    aliases: dict[str, str],
    settings: AnsibleSettings,
    concurrency: int,
) -> RefreshResult:
    github_settings = get_config_section("github", GithubSettings)
    core = get_core_settings()
    with GithubClient(github_settings, http=core.http) as github:
        token = (
            github_settings.token.get_secret_value().strip()
            if github_settings.token is not None
            else ""
        )
        result = RefreshGitSourceIndex(
            github=github,
            git=GitRepositoryCache(),
            index=index,
            aliases=aliases,
            default_dependency_paths=settings.dependency_paths,
            repo_cache_path=settings.repo_cache_path,
            clone_protocol=settings.git_clone_protocol,
            fetch_depth=settings.git_fetch_depth,
            blob_filter=settings.git_blob_filter,
            auth_header=_git_auth_header(token) if token else None,
            concurrency=concurrency,
            ref_scan_default=settings.ref_scan_default,
        )(source, source_key=source_key)
    return result


def _refresh_summary(
    action: str,
    label: str,
    result: RefreshResult,
    *,
    concurrency: int,
    elapsed: float,
) -> str:
    message = (
        f"{action} {label}: {result.repos} repos, {result.refs} refs, {result.edges} edges, "
        f"{result.changed_refs} changed, {result.unchanged_refs} unchanged in {elapsed:.2f}s"
    )
    return f"{message} (concurrency {concurrency})"


def _git_auth_header(token: str) -> str:
    credential = b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {credential}"


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
