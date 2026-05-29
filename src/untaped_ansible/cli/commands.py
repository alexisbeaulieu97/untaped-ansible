"""Typer commands for Ansible dependency graphing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer
from untaped import (
    ColumnsOption,
    FormatOption,
    ProfileOverrideOption,
    UntapedError,
    format_output,
    get_config_section,
    get_core_settings,
    profile_override,
    report_errors,
)
from untaped_github import GithubClient, GithubSettings

from untaped_ansible.application import BuildGraph, GraphRequest
from untaped_ansible.application.ports import DependencyIndex, IndexedDependency
from untaped_ansible.application.refresh_index import RefreshIndex
from untaped_ansible.domain.graph import DependencyGraph
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.models import DependencyDeclaration
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.domain.renderers import GraphFormat, render_graph
from untaped_ansible.infrastructure import (
    AliasRepository,
    GithubDependencyIndex,
    ScopeRepository,
    SqliteDependencyIndex,
)
from untaped_ansible.settings import AnsibleSettings, ScopeDefinition, normalize_team_refs

GraphDirectionOption = Annotated[
    Literal["deps", "impact", "both"],
    typer.Option("--direction", help="Graph direction to include."),
]
GraphFormatOption = Annotated[
    GraphFormat,
    typer.Option("--format", "-f", help="Graph output format."),
]

app = typer.Typer(name="ansible", help="Analyze Ansible dependency graphs.", no_args_is_help=True)
alias_app = typer.Typer(name="alias", help="Manage dependency aliases.", no_args_is_help=True)
scope_app = typer.Typer(name="scope", help="Manage dependency index scopes.", no_args_is_help=True)
index_app = typer.Typer(name="index", help="Manage the dependency index.", no_args_is_help=True)


@app.callback()
def _callback() -> None:
    """Analyze Ansible dependency graphs."""


app.add_typer(alias_app, name="alias")
app.add_typer(scope_app, name="scope")
app.add_typer(index_app, name="index")


@app.command("graph", no_args_is_help=True)
def graph_command(
    target: str = typer.Argument(..., help="Target repo, GitHub URL, or local path."),
    ref: str | None = typer.Option(None, "--ref", help="Target branch, tag, or SHA."),
    to_ref: str | None = typer.Option(None, "--to-ref", help="Reserved for old/new comparison."),
    scope: str | None = typer.Option(None, "--scope", help="Named impact index scope."),
    direction: GraphDirectionOption = "both",
    depth: str = typer.Option("3", "--depth", help="Traversal depth or 'unlimited'."),
    kind: Literal["auto", "playbook", "role"] = typer.Option(
        "auto",
        "--kind",
        help="Target kind hint.",
    ),
    repo: str | None = typer.Option(None, "--repo", help="Canonical owner/repo override."),
    fmt: GraphFormatOption = "tree",
    profile: ProfileOverrideOption = None,
) -> None:
    """Render dependency and impact graph for a target."""
    del kind
    with report_errors(), profile_override(profile):
        settings = get_config_section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        target_repo = repo or _resolve_target_repo(target, aliases)
        if target_repo is None:
            raise UntapedError(f"could not resolve target to a GitHub repo: {target!r}")
        index: DependencyIndex = SqliteDependencyIndex(settings.index_path)
        target_path = Path(target).expanduser()
        if target_path.exists():
            local_edges = _local_dependencies(
                target_path,
                repo=target_repo,
                ref=to_ref or ref,
                aliases=aliases,
                dependency_paths=settings.dependency_paths,
            )
            index = _OverlayIndex(index, local_edges)
            graph = _graph_from_index(
                index,
                repo=target_repo,
                ref=ref,
                to_ref=to_ref,
                scope=scope,
                direction=direction,
                depth=depth,
                stale_after=settings.stale_after,
            )
        elif _should_read_live_dependencies(
            target=target,
            scope=scope,
            direction=direction,
        ):
            github_settings = get_config_section("github", GithubSettings)
            core = get_core_settings()
            with GithubClient(github_settings, http=core.http) as github:
                graph_direction = _live_graph_direction(scope=scope, direction=direction)
                index = GithubDependencyIndex(
                    github=github,
                    wrapped=index,
                    aliases=aliases,
                    dependency_paths=settings.dependency_paths,
                )
                graph = _graph_from_index(
                    index,
                    repo=target_repo,
                    ref=ref,
                    to_ref=to_ref,
                    scope=scope,
                    direction=graph_direction,
                    depth=depth,
                    stale_after=settings.stale_after,
                )
        else:
            graph = _graph_from_index(
                index,
                repo=target_repo,
                ref=ref,
                to_ref=to_ref,
                scope=scope,
                direction=direction,
                depth=depth,
                stale_after=settings.stale_after,
            )
        typer.echo(render_graph(graph, fmt))


@alias_app.command("add", no_args_is_help=True)
def alias_add_command(alias: str, repo: str) -> None:
    """Map an Ansible role/Galaxy name to a GitHub owner/repo."""
    with report_errors():
        AliasRepository().set(alias, repo)
        typer.echo(f"set alias {alias!r} -> {repo}", err=True)


@alias_app.command("list")
def alias_list_command(fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List dependency aliases."""
    with report_errors():
        rows: list[dict[str, object]] = [
            {"alias": alias, "repo": repo}
            for alias, repo in sorted(AliasRepository().entries().items())
        ]
        typer.echo(format_output(rows, fmt=fmt, columns=columns))


@alias_app.command("remove", no_args_is_help=True)
def alias_remove_command(alias: str) -> None:
    """Remove a dependency alias."""
    with report_errors():
        removed = AliasRepository().remove(alias)
        if not removed:
            raise UntapedError(f"unknown alias: {alias!r}")
        typer.echo(f"removed alias {alias!r}", err=True)


@scope_app.command("add", no_args_is_help=True)
def scope_add_command(
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
        help="Ref namespace to scan: heads or tags.",
    ),
    ref_patterns: list[str] | None = typer.Option(
        None,
        "--ref-pattern",
        help="fnmatch pattern for branch/tag names.",
    ),
) -> None:
    """Create or replace a named index scope."""
    with report_errors():
        normalized_orgs = orgs or []
        try:
            normalized_teams = normalize_team_refs(teams or [], normalized_orgs)
        except ValueError as exc:
            raise UntapedError(str(exc)) from exc
        scope = ScopeDefinition(
            name=name,
            orgs=normalized_orgs,
            teams=normalized_teams,
            repos=repos or [],
            dependency_paths=paths or [],
            ref_kinds=ref_kinds or ["heads", "tags"],
            ref_patterns=ref_patterns or [],
        )
        ScopeRepository().upsert(scope)
        typer.echo(f"saved scope {name!r}", err=True)


@scope_app.command("list")
def scope_list_command(fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List named index scopes."""
    with report_errors():
        rows = [_scope_row(scope) for scope in ScopeRepository().entries()]
        typer.echo(format_output(rows, fmt=fmt, columns=columns))


@scope_app.command("show", no_args_is_help=True)
def scope_show_command(
    name: str,
    fmt: FormatOption = "table",
    columns: ColumnsOption = None,
) -> None:
    """Show one named index scope."""
    with report_errors():
        scope = ScopeRepository().get(name)
        if scope is None:
            raise UntapedError(f"unknown scope: {name!r}")
        typer.echo(format_output([_scope_row(scope)], fmt=fmt, columns=columns))


@scope_app.command("remove", no_args_is_help=True)
def scope_remove_command(name: str) -> None:
    """Remove a named index scope."""
    with report_errors():
        removed = ScopeRepository().remove(name)
        if not removed:
            raise UntapedError(f"unknown scope: {name!r}")
        typer.echo(f"removed scope {name!r}", err=True)


@index_app.command("status")
def index_status_command(
    scope: str | None = typer.Option(None, "--scope", help="Scope to inspect."),
    fmt: FormatOption = "table",
    columns: ColumnsOption = None,
    profile: ProfileOverrideOption = None,
) -> None:
    """Show index status."""
    with report_errors(), profile_override(profile):
        settings = get_config_section("ansible", AnsibleSettings)
        index = SqliteDependencyIndex(settings.index_path)
        statuses = []
        scopes = [scope] if scope is not None else [s.name for s in ScopeRepository().entries()]
        for name in scopes:
            status = index.status(name)
            if status is not None:
                statuses.append(status.model_dump())
        typer.echo(format_output(statuses, fmt=fmt, columns=columns))


@index_app.command("clear")
def index_clear_command(
    scope: str | None = typer.Option(None, "--scope", help="Scope to clear."),
    profile: ProfileOverrideOption = None,
) -> None:
    """Clear indexed dependency data."""
    with report_errors(), profile_override(profile):
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(scope)
        target = scope or "all scopes"
        typer.echo(f"cleared index for {target}", err=True)


@index_app.command("refresh")
def index_refresh_command(
    scope: str = typer.Option(..., "--scope", help="Scope to refresh."),
    profile: ProfileOverrideOption = None,
) -> None:
    """Refresh a named scope from GitHub."""
    with report_errors(), profile_override(profile):
        scope_definition = ScopeRepository().get(scope)
        if scope_definition is None:
            raise UntapedError(f"unknown scope: {scope!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        github_settings = get_config_section("github", GithubSettings)
        core = get_core_settings()
        with GithubClient(github_settings, http=core.http) as github:
            result = RefreshIndex(
                github=github,
                index=SqliteDependencyIndex(settings.index_path),
                aliases=aliases,
                default_dependency_paths=settings.dependency_paths,
            )(scope_definition)
        typer.echo(
            f"refreshed scope {scope!r}: {result.repos} repos, "
            f"{result.refs} refs, {result.edges} edges",
            err=True,
        )


def _scope_row(scope: ScopeDefinition) -> dict[str, object]:
    return scope.model_dump()


def _resolve_target_repo(target: str, aliases: dict[str, str]) -> str | None:
    path = Path(target).expanduser()
    if path.exists():
        return _repo_from_local_git(path)
    declaration = DependencyDeclaration(name=target, src=target, source_path="<target>")
    return IdentityResolver(aliases).resolve(declaration).repo


def _repo_from_local_git(path: Path) -> str | None:
    git_config = _git_config_path(path)
    if not git_config.is_file():
        return None
    for line in git_config.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("url = "):
            value = stripped.removeprefix("url = ").strip()
            declaration = DependencyDeclaration(name=value, src=value, source_path="<git-remote>")
            return IdentityResolver().resolve(declaration).repo
    return None


def _git_config_path(path: Path) -> Path:
    dot_git = path / ".git"
    if dot_git.is_dir():
        return dot_git / "config"
    if dot_git.is_file():
        first_line = dot_git.read_text().splitlines()[0]
        if first_line.startswith("gitdir: "):
            gitdir = Path(first_line.removeprefix("gitdir: ").strip())
            if not gitdir.is_absolute():
                gitdir = path / gitdir
            return gitdir / "config"
    return dot_git / "config"


def _local_dependencies(
    path: Path,
    *,
    repo: str,
    ref: str | None,
    aliases: dict[str, str],
    dependency_paths: list[str],
) -> list[IndexedDependency]:
    edges: list[IndexedDependency] = []
    resolver = IdentityResolver(aliases)
    for relative in dependency_paths:
        dep_path = path / relative
        if not dep_path.is_file():
            continue
        report = parse_dependency_file(relative, dep_path.read_text())
        for declaration in report.dependencies:
            resolved = resolver.resolve(declaration)
            edges.append(
                IndexedDependency(
                    source_repo=repo,
                    source_ref=ref,
                    dependency_repo=resolved.repo,
                    dependency_name=declaration.name,
                    dependency_version=declaration.version,
                    source_path=relative,
                    unresolved=resolved.unresolved,
                )
            )
    return edges


def _build_graph(
    index: DependencyIndex, request: GraphRequest, *, old_ref: str | None
) -> DependencyGraph:
    if old_ref is None or request.direction == "deps":
        return BuildGraph(index)(request)
    if request.direction == "impact":
        return BuildGraph(index)(request.model_copy(update={"ref": old_ref}))
    deps_graph = BuildGraph(index)(request.model_copy(update={"direction": "deps"}))
    impact_graph = BuildGraph(index)(
        request.model_copy(update={"ref": old_ref, "direction": "impact"})
    )
    node_map = {node.id: node for node in (*deps_graph.nodes, *impact_graph.nodes)}
    edge_map = {
        (edge.source_id, edge.target_id, edge.relation): edge
        for edge in (*deps_graph.edges, *impact_graph.edges)
    }
    return DependencyGraph(
        target_id=deps_graph.target_id,
        nodes=tuple(node_map.values()),
        edges=tuple(edge_map.values()),
        warnings=tuple(dict.fromkeys((*deps_graph.warnings, *impact_graph.warnings))),
    )


def _graph_from_index(
    index: DependencyIndex,
    *,
    repo: str,
    ref: str | None,
    to_ref: str | None,
    scope: str | None,
    direction: Literal["deps", "impact", "both"],
    depth: str,
    stale_after: int,
) -> DependencyGraph:
    return _build_graph(
        index,
        GraphRequest(
            repo=repo,
            ref=to_ref or ref,
            scope=scope,
            direction=direction,
            depth=_parse_depth(depth),
            stale_after=stale_after,
        ),
        old_ref=ref if to_ref is not None else None,
    )


def _should_read_live_dependencies(
    *,
    target: str,
    scope: str | None,
    direction: Literal["deps", "impact", "both"],
) -> bool:
    if direction == "impact":
        return False
    return scope is None or target.startswith(("https://github.com/", "git@github.com:"))


def _live_graph_direction(
    *,
    scope: str | None,
    direction: Literal["deps", "impact", "both"],
) -> Literal["deps", "impact", "both"]:
    if scope is None and direction == "both":
        return "deps"
    return direction


def _parse_depth(value: str) -> int | None:
    if value == "unlimited":
        return None
    try:
        depth = int(value)
    except ValueError as exc:
        raise typer.BadParameter("--depth must be an integer or 'unlimited'") from exc
    if depth < 0:
        raise typer.BadParameter("--depth must be >= 0")
    return depth


class _OverlayIndex:
    def __init__(self, wrapped: DependencyIndex, local_edges: list[IndexedDependency]) -> None:
        self._wrapped = wrapped
        self._local_edges = local_edges

    def dependencies(
        self,
        repo: str,
        ref: str | None,
        *,
        scope: str | None,
    ) -> list[IndexedDependency]:
        local = [
            edge
            for edge in self._local_edges
            if edge.source_repo == repo and edge.source_ref == ref
        ]
        return local or self._wrapped.dependencies(repo, ref, scope=scope)

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        scope: str | None,
    ) -> list[IndexedDependency]:
        return self._wrapped.dependents(repo, ref, scope=scope)

    def is_stale(self, scope: str | None, *, max_age_seconds: int) -> bool:
        return self._wrapped.is_stale(scope, max_age_seconds=max_age_seconds)
