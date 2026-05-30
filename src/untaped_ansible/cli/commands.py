"""Typer commands for Ansible dependency graphing."""

from __future__ import annotations

import hashlib
import json
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
from untaped_ansible.application.refresh_index import RefreshResult, RefreshSourceIndex
from untaped_ansible.domain.graph import DependencyGraph
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.models import DependencyDeclaration
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.domain.renderers import GraphFormat, render_graph
from untaped_ansible.infrastructure import (
    AliasRepository,
    GithubDependencyIndex,
    SourceRepository,
    SqliteDependencyIndex,
)
from untaped_ansible.settings import AnsibleSettings, SourceDefinition, normalize_team_refs

GraphDirection = Literal["deps", "impact", "both"]
GraphFormatOption = Annotated[
    GraphFormat,
    typer.Option("--format", "-f", help="Graph output format."),
]

app = typer.Typer(name="ansible", help="Analyze Ansible dependency graphs.", no_args_is_help=True)
alias_app = typer.Typer(name="alias", help="Manage dependency aliases.", no_args_is_help=True)
source_app = typer.Typer(
    name="source",
    help="Manage reusable GitHub sources.",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    """Analyze Ansible dependency graphs."""


app.add_typer(alias_app, name="alias")
app.add_typer(source_app, name="source")


@app.command(
    "graph",
    no_args_is_help=True,
    epilog=(
        "Examples:\n"
        "  untaped ansible graph acme/site --downstream\n"
        "  untaped ansible graph acme/base --org acme --team platform --upstream --refresh\n"
        "  untaped ansible source save platform --org acme --team platform\n"
        "  untaped ansible graph acme/base --source platform --upstream\n"
        "  untaped ansible graph acme/base --source platform --both --format mermaid "
        "--output deps.mmd"
    ),
)
def graph_command(
    target: str = typer.Argument(..., help="Target repo, GitHub URL, alias, or local path."),
    ref: str | None = typer.Option(
        None,
        "--ref",
        help="Target branch, tag, or SHA for live dependency reads and cached upstream lookup.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Saved source to use for upstream impact.",
    ),
    upstream: bool = typer.Option(False, "--upstream", help="Show who depends on TARGET."),
    downstream: bool = typer.Option(False, "--downstream", help="Show what TARGET depends on."),
    both: bool = typer.Option(False, "--both", help="Show upstream and downstream."),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh source data before graphing."),
    depth: str = typer.Option("3", "--depth", help="Traversal depth or 'unlimited'."),
    kind: Literal["auto", "playbook", "role"] = typer.Option(
        "auto",
        "--kind",
        help="Target kind hint.",
    ),
    target_repo: str | None = typer.Option(
        None,
        "--target-repo",
        help="Canonical owner/repo override for local targets.",
    ),
    orgs: list[str] | None = typer.Option(None, "--org", help="Inline source GitHub org."),
    teams: list[str] | None = typer.Option(
        None,
        "--team",
        help="Inline source GitHub team slug with one --org, or ORG/SLUG.",
    ),
    source_repos: list[str] | None = typer.Option(
        None,
        "--repo",
        help="Inline source GitHub repo as owner/name.",
    ),
    paths: list[str] | None = typer.Option(None, "--path", help="Inline source dependency path."),
    ref_kinds: list[str] | None = typer.Option(
        None,
        "--ref-kind",
        help="Inline source ref namespace to scan: heads or tags.",
    ),
    ref_patterns: list[str] | None = typer.Option(
        None,
        "--ref-pattern",
        help="Inline source fnmatch pattern for branch/tag names.",
    ),
    fmt: GraphFormatOption = "tree",
    output: Path | None = typer.Option(None, "--output", help="Write graph data to a file."),
    profile: ProfileOverrideOption = None,
) -> None:
    """Graph Ansible dependency relationships for a role, repo, or playbook."""
    del kind
    with report_errors(), profile_override(profile):
        settings = get_config_section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        target_repo_name = target_repo or _resolve_target_repo(target, aliases)
        if target_repo_name is None:
            raise UntapedError(f"could not resolve target to a GitHub repo: {target!r}")

        direction = _graph_direction(upstream=upstream, downstream=downstream, both=both)
        graph_source = _graph_source(
            source_name=source,
            orgs=orgs,
            teams=teams,
            repos=source_repos,
            paths=paths,
            ref_kinds=ref_kinds,
            ref_patterns=ref_patterns,
        )
        sqlite_index = SqliteDependencyIndex(settings.index_path)
        if refresh:
            if graph_source.definition is None:
                raise typer.BadParameter("--refresh requires --source or inline source selectors")
            if graph_source.key is None or graph_source.label is None:
                raise typer.BadParameter("--refresh requires --source or inline source selectors")
            result = _refresh_source(
                graph_source.definition,
                source_key=graph_source.key,
                index=sqlite_index,
                aliases=aliases,
                dependency_paths=settings.dependency_paths,
            )
            typer.echo(
                f"refreshed {graph_source.label}: {result.repos} repos, "
                f"{result.refs} refs, {result.edges} edges",
                err=True,
            )

        direction, graph_warnings = _effective_direction(
            target=target,
            source_state=graph_source,
            index=sqlite_index,
            direction=direction,
        )

        index: DependencyIndex = sqlite_index
        target_path = Path(target).expanduser()
        if target_path.exists():
            local_edges = _local_dependencies(
                target_path,
                repo=target_repo_name,
                ref=ref,
                aliases=aliases,
                dependency_paths=settings.dependency_paths,
            )
            index = _OverlayIndex(index, local_edges)
            graph = _graph_from_index(
                index,
                repo=target_repo_name,
                ref=ref,
                source_key=graph_source.key,
                direction=direction,
                depth=depth,
                stale_after=settings.stale_after,
            )
        else:
            github_settings = get_config_section("github", GithubSettings)
            if _should_use_live_dependencies(
                direction=direction,
                source_key=graph_source.key,
                github_settings=github_settings,
            ):
                core = get_core_settings()
                with GithubClient(github_settings, http=core.http) as github:
                    index = GithubDependencyIndex(
                        github=github,
                        wrapped=index,
                        aliases=aliases,
                        dependency_paths=settings.dependency_paths,
                    )
                    graph = _graph_from_index(
                        index,
                        repo=target_repo_name,
                        ref=ref,
                        source_key=graph_source.key,
                        direction=direction,
                        depth=depth,
                        stale_after=settings.stale_after,
                    )
            else:
                graph = _graph_from_index(
                    index,
                    repo=target_repo_name,
                    ref=ref,
                    source_key=graph_source.key,
                    direction=direction,
                    depth=depth,
                    stale_after=settings.stale_after,
                )

        graph = _with_graph_warnings(
            graph,
            [
                *graph_warnings,
                *_empty_graph_warnings(
                    graph,
                    direction=direction,
                    dependency_paths=settings.dependency_paths,
                    source_label=graph_source.label,
                ),
            ],
        )
        _emit_graph(graph, fmt=fmt, output=output)


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


@source_app.command("save", no_args_is_help=True)
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
        help="Ref namespace to scan: heads or tags.",
    ),
    ref_patterns: list[str] | None = typer.Option(
        None,
        "--ref-pattern",
        help="fnmatch pattern for branch/tag names.",
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
        SourceRepository().upsert(source)
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        typer.echo(f"saved source {name!r}", err=True)


@source_app.command("list")
def source_list_command(fmt: FormatOption = "table", columns: ColumnsOption = None) -> None:
    """List saved sources."""
    with report_errors():
        rows = [_source_row(source) for source in SourceRepository().entries()]
        typer.echo(format_output(rows, fmt=fmt, columns=columns))


@source_app.command("show", no_args_is_help=True)
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
        typer.echo(format_output([_source_row(source)], fmt=fmt, columns=columns))


@source_app.command("remove", no_args_is_help=True)
def source_remove_command(name: str) -> None:
    """Remove a saved source."""
    with report_errors():
        removed = SourceRepository().remove(name)
        if not removed:
            raise UntapedError(f"unknown source: {name!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        SqliteDependencyIndex(settings.index_path).clear(_saved_source_key(name))
        typer.echo(f"removed source {name!r}", err=True)


@source_app.command("status")
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
        typer.echo(format_output(rows, fmt=fmt, columns=columns))


@source_app.command("refresh", no_args_is_help=True)
def source_refresh_command(
    name: str,
    profile: ProfileOverrideOption = None,
) -> None:
    """Refresh a saved source from GitHub."""
    with report_errors(), profile_override(profile):
        source = SourceRepository().get(name)
        if source is None:
            raise UntapedError(f"unknown source: {name!r}")
        settings = get_config_section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        result = _refresh_source(
            source,
            source_key=_saved_source_key(name),
            index=SqliteDependencyIndex(settings.index_path),
            aliases=aliases,
            dependency_paths=settings.dependency_paths,
        )
        typer.echo(
            f"refreshed source {name!r}: {result.repos} repos, "
            f"{result.refs} refs, {result.edges} edges",
            err=True,
        )


class _GraphSource:
    def __init__(
        self,
        *,
        definition: SourceDefinition | None,
        key: str | None,
        label: str | None,
        saved: bool,
    ) -> None:
        self.definition = definition
        self.key = key
        self.label = label
        self.saved = saved


def _graph_direction(*, upstream: bool, downstream: bool, both: bool) -> GraphDirection:
    selected = [upstream, downstream, both].count(True)
    if selected > 1:
        raise typer.BadParameter("choose only one of --upstream, --downstream, or --both")
    if upstream:
        return "impact"
    if downstream:
        return "deps"
    return "both"


def _graph_source(
    *,
    source_name: str | None,
    orgs: list[str] | None,
    teams: list[str] | None,
    repos: list[str] | None,
    paths: list[str] | None,
    ref_kinds: list[str] | None,
    ref_patterns: list[str] | None,
) -> _GraphSource:
    has_inline = any((orgs, teams, repos, paths, ref_kinds, ref_patterns))
    if source_name is not None and has_inline:
        raise typer.BadParameter(
            "--source cannot be combined with --org, --team, --repo, --path, "
            "--ref-kind, or --ref-pattern"
        )
    if source_name is not None:
        source = SourceRepository().get(source_name)
        if source is None:
            raise UntapedError(f"unknown source: {source_name!r}")
        return _GraphSource(
            definition=source,
            key=_saved_source_key(source_name),
            label=source_name,
            saved=True,
        )
    if has_inline:
        source = _source_definition(
            name="<inline>",
            orgs=orgs,
            teams=teams,
            repos=repos,
            paths=paths,
            ref_kinds=ref_kinds,
            ref_patterns=ref_patterns,
        )
        key = _inline_source_key(source)
        return _GraphSource(
            definition=source,
            key=key,
            label=f"inline source {key.removeprefix('inline:')}",
            saved=False,
        )
    return _GraphSource(definition=None, key=None, label=None, saved=False)


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
    normalized_orgs = orgs or []
    try:
        normalized_teams = normalize_team_refs(teams or [], normalized_orgs)
    except ValueError as exc:
        raise UntapedError(str(exc)) from exc
    normalized_repos = repos or []
    if not any((normalized_orgs, normalized_teams, normalized_repos)):
        raise UntapedError("source requires --org, --team, or --repo")
    for repo in normalized_repos:
        if not _is_repo_name(repo):
            raise UntapedError(f"repo must be owner/name: {repo!r}")
    normalized_ref_kinds = ref_kinds or ["heads", "tags"]
    invalid_ref_kinds = sorted(set(normalized_ref_kinds) - {"heads", "tags"})
    if invalid_ref_kinds:
        raise UntapedError("ref-kind must be heads or tags")
    return SourceDefinition(
        name=name,
        orgs=normalized_orgs,
        teams=normalized_teams,
        repos=normalized_repos,
        dependency_paths=paths or [],
        ref_kinds=normalized_ref_kinds,
        ref_patterns=ref_patterns or [],
    )


def _is_repo_name(value: str) -> bool:
    owner, separator, repo = value.partition("/")
    return bool(owner and separator and repo and "/" not in repo)


def _source_row(source: SourceDefinition) -> dict[str, object]:
    return source.model_dump()


def _saved_source_key(name: str) -> str:
    return f"source:{name}"


def _inline_source_key(source: SourceDefinition) -> str:
    payload = source.model_dump(exclude={"name"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"inline:{hashlib.sha256(encoded).hexdigest()[:16]}"


def _refresh_source(
    source: SourceDefinition,
    *,
    source_key: str,
    index: SqliteDependencyIndex,
    aliases: dict[str, str],
    dependency_paths: list[str],
) -> RefreshResult:
    github_settings = get_config_section("github", GithubSettings)
    core = get_core_settings()
    with GithubClient(github_settings, http=core.http) as github:
        result = RefreshSourceIndex(
            github=github,
            index=index,
            aliases=aliases,
            default_dependency_paths=dependency_paths,
        )(source, source_key=source_key)
    return result


def _effective_direction(
    *,
    target: str,
    source_state: _GraphSource,
    index: SqliteDependencyIndex,
    direction: GraphDirection,
) -> tuple[GraphDirection, list[str]]:
    if direction == "deps":
        return direction, []
    if source_state.key is None:
        message = (
            "upstream requires --source NAME or inline selectors like --org, --team, or --repo"
        )
        if direction == "impact":
            raise UntapedError(message)
        return "deps", ["upstream omitted: pass --source NAME or inline selectors"]
    if index.status(source_state.key) is not None:
        return direction, []
    message = _missing_source_index_message(target, source_state)
    if direction == "impact":
        raise UntapedError(message)
    return "deps", [f"upstream omitted: {message}"]


def _missing_source_index_message(target: str, source_state: _GraphSource) -> str:
    if source_state.saved:
        label = source_state.label or "unknown"
        return (
            f"no cached source data found for source {label!r}. Run: "
            f"`untaped ansible source refresh {label}`. Or re-run graph with: "
            f"`untaped ansible graph {target} --source {label} --upstream --refresh`."
        )
    return (
        "no cached source data found for inline source. Re-run this graph command with "
        "`--refresh` to scan GitHub and cache the result."
    )


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


def _graph_from_index(
    index: DependencyIndex,
    *,
    repo: str,
    ref: str | None,
    source_key: str | None,
    direction: GraphDirection,
    depth: str,
    stale_after: int,
) -> DependencyGraph:
    return BuildGraph(index)(
        GraphRequest(
            repo=repo,
            ref=ref,
            source_key=source_key,
            direction=direction,
            depth=_parse_depth(depth),
            stale_after=stale_after,
        )
    )


def _with_graph_warnings(graph: DependencyGraph, warnings: list[str]) -> DependencyGraph:
    if not warnings:
        return graph
    return graph.model_copy(update={"warnings": tuple(dict.fromkeys((*graph.warnings, *warnings)))})


def _empty_graph_warnings(
    graph: DependencyGraph,
    *,
    direction: GraphDirection,
    dependency_paths: list[str],
    source_label: str | None,
) -> list[str]:
    if graph.edges:
        return []
    paths = ", ".join(dependency_paths)
    if direction == "deps":
        return [
            f"no declared downstream dependencies found for {graph.target_id}; "
            f"checked configured dependency paths: {paths}"
        ]
    if direction == "impact":
        label = source_label or "source"
        return [f"no cached upstream dependents found for {graph.target_id} in {label}"]
    label = source_label or "source"
    return [
        f"no declared downstream dependencies or cached upstream dependents found for "
        f"{graph.target_id} in {label}; checked configured dependency paths: {paths}"
    ]


def _should_use_live_dependencies(
    *,
    direction: GraphDirection,
    source_key: str | None,
    github_settings: GithubSettings,
) -> bool:
    if direction == "impact":
        return False
    return source_key is None or _has_github_token(github_settings)


def _has_github_token(settings: GithubSettings) -> bool:
    if settings.token is None:
        return False
    return bool(settings.token.get_secret_value().strip())


def _emit_graph(graph: DependencyGraph, *, fmt: GraphFormat, output: Path | None) -> None:
    rendered = render_graph(graph, fmt)
    if output is None:
        typer.echo(rendered)
        return
    output.expanduser().parent.mkdir(parents=True, exist_ok=True)
    output.expanduser().write_text(rendered)


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
        source_key: str | None,
    ) -> list[IndexedDependency]:
        local = [
            edge
            for edge in self._local_edges
            if edge.source_repo == repo and edge.source_ref == ref
        ]
        return local or self._wrapped.dependencies(repo, ref, source_key=source_key)

    def dependents(
        self,
        repo: str,
        ref: str | None,
        *,
        source_key: str | None,
    ) -> list[IndexedDependency]:
        return self._wrapped.dependents(repo, ref, source_key=source_key)

    def is_stale(self, source_key: str | None, *, max_age_seconds: int) -> bool:
        return self._wrapped.is_stale(source_key, max_age_seconds=max_age_seconds)
