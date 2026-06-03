"""Graph command and graph-specific CLI helpers for the Ansible plugin."""

from __future__ import annotations

import time
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from untaped import (
    ProfileOverrideOption,
    UntapedError,
    get_config_section,
    get_core_settings,
    profile_override,
    report_errors,
)
from untaped_github import GithubClient, GithubSettings

import untaped_ansible.cli.source_commands as source_commands
from untaped_ansible.application import BuildGraph, GraphRequest
from untaped_ansible.application.ports import DependencyIndex
from untaped_ansible.domain.graph import DependencyGraph
from untaped_ansible.domain.identity import IdentityResolver
from untaped_ansible.domain.models import DependencyDeclaration
from untaped_ansible.domain.parser import parse_dependency_file
from untaped_ansible.domain.payloads import IndexedDependency
from untaped_ansible.domain.renderers import GraphFormat, render_graph
from untaped_ansible.infrastructure import (
    AliasRepository,
    GithubDependencyIndex,
    MultiSourceDependencyIndex,
    OverlayDependencyIndex,
    SourceRepository,
    SqliteDependencyIndex,
)
from untaped_ansible.settings import AnsibleSettings, SourceDefinition

GraphDirection = Literal["deps", "impact", "both"]
CacheBackend = source_commands.CacheBackend
GraphFormatOption = Annotated[
    GraphFormat,
    typer.Option("--format", "-f", help="Graph output format."),
]


def register_graph_command(app: typer.Typer) -> None:
    """Register graph commands on the Ansible root app."""
    app.command(
        "graph",
        no_args_is_help=True,
        epilog=(
            "Examples:\n"
            "  untaped ansible graph acme/site --downstream\n"
            "  untaped ansible graph acme/base --org acme --team platform --upstream --refresh\n"
            "  untaped ansible source save platform --org acme --team platform\n"
            "  untaped ansible graph acme/base --source platform --upstream\n"
            "  untaped ansible graph acme/site --source platform --downstream --live\n"
            "  untaped ansible graph acme/base --source platform --both --format mermaid "
            "--output deps.mmd"
        ),
    )(graph_command)


def graph_command(
    target: str = typer.Argument(..., help="Target repo, GitHub URL, alias, or local path."),
    ref: str | None = typer.Option(
        None,
        "--ref",
        help="Target branch, tag, or SHA for live dependency reads and cached upstream lookup.",
    ),
    source: list[str] | None = typer.Option(
        None,
        "--source",
        help="Saved source to use for cached graph data and upstream impact; repeat to union.",
    ),
    upstream: bool = typer.Option(False, "--upstream", help="Show who depends on TARGET."),
    downstream: bool = typer.Option(False, "--downstream", help="Show what TARGET depends on."),
    both: bool = typer.Option(False, "--both", help="Show upstream and downstream (default)."),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh source data before graphing."),
    cached: bool = typer.Option(
        False,
        "--cached",
        help="Use cached source data without checking remote refs.",
    ),
    cache_backend: CacheBackend | None = typer.Option(
        None,
        "--cache-backend",
        help="Source refresh backend: git or api; defaults to ansible.cache_backend.",
    ),
    concurrency: int | None = typer.Option(
        None,
        "--concurrency",
        min=1,
        max=32,
        help="Git-backed source refresh concurrency; defaults to ansible.git_fetch_concurrency.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Use live GitHub reads for downstream graphing even when source data is configured.",
    ),
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
        help="Inline source ref namespace to scan: heads or tags; omit for configured default.",
    ),
    ref_patterns: list[str] | None = typer.Option(
        None,
        "--ref-pattern",
        help="Inline source fnmatch pattern for branch/tag names; omit for configured default.",
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
        if cached and refresh:
            raise typer.BadParameter("--cached cannot be combined with --refresh")
        selected_backend = cache_backend or settings.cache_backend
        git_concurrency = concurrency or settings.git_fetch_concurrency
        graph_source = _graph_source(
            source_names=source,
            orgs=orgs,
            teams=teams,
            repos=source_repos,
            paths=paths,
            ref_kinds=ref_kinds,
            ref_patterns=ref_patterns,
        )
        sqlite_index = SqliteDependencyIndex(settings.index_path)
        index: DependencyIndex = _dependency_index_for_graph_source(sqlite_index, graph_source)
        should_refresh_source = _should_refresh_source(
            source_state=graph_source,
            direction=direction,
            cached=cached,
            live=live,
            refresh=refresh,
        )
        if should_refresh_source:
            if not graph_source.selections:
                raise typer.BadParameter("--refresh requires --source or inline source selectors")
            for selection in graph_source.selections:
                started_at = time.perf_counter()
                result = source_commands._refresh_source(
                    selection.definition,
                    source_key=selection.key,
                    index=sqlite_index,
                    aliases=aliases,
                    settings=settings,
                    cache_backend=selected_backend,
                    concurrency=git_concurrency,
                )
                typer.echo(
                    source_commands._refresh_summary(
                        "refreshed" if refresh else "checked",
                        selection.label,
                        result,
                        cache_backend=selected_backend,
                        concurrency=git_concurrency,
                        elapsed=time.perf_counter() - started_at,
                    ),
                    err=True,
                )

        direction, graph_warnings = _effective_direction(
            target=target,
            source_state=graph_source,
            index=sqlite_index,
            direction=direction,
            cached=cached,
        )

        target_path = Path(target).expanduser()
        if target_path.exists():
            local_edges = _local_dependencies(
                target_path,
                repo=target_repo_name,
                ref=ref,
                aliases=aliases,
                dependency_paths=settings.dependency_paths,
            )
            index = OverlayDependencyIndex(
                index,
                local_edges,
                authoritative_sources={(target_repo_name, ref)},
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
            github_settings = get_config_section("github", GithubSettings)
            if _should_use_live_dependencies(
                direction=direction,
                source_key=graph_source.key,
                live=live,
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


@dataclass(frozen=True)
class _GraphSourceSelection:
    definition: SourceDefinition
    key: str
    label: str


@dataclass(frozen=True)
class _GraphSource:
    selections: tuple[_GraphSourceSelection, ...]
    key: str | None
    label: str | None
    saved: bool


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
    source_names: list[str] | None,
    orgs: list[str] | None,
    teams: list[str] | None,
    repos: list[str] | None,
    paths: list[str] | None,
    ref_kinds: list[str] | None,
    ref_patterns: list[str] | None,
) -> _GraphSource:
    has_inline = any((orgs, teams, repos, paths, ref_kinds, ref_patterns))
    selected_source_names = _dedupe_preserve_order(source_names or [])
    if selected_source_names and has_inline:
        raise typer.BadParameter(
            "--source cannot be combined with --org, --team, --repo, --path, "
            "--ref-kind, or --ref-pattern"
        )
    if selected_source_names:
        source_repository = SourceRepository()
        selections: list[_GraphSourceSelection] = []
        for source_name in selected_source_names:
            source = source_repository.get(source_name)
            if source is None:
                raise UntapedError(f"unknown source: {source_name!r}")
            selections.append(
                _GraphSourceSelection(
                    definition=source,
                    key=source_commands._saved_source_key(source_name),
                    label=source_name,
                )
            )
        return _GraphSource(
            selections=tuple(selections),
            key=_graph_source_key(selections),
            label=_graph_source_label(selections),
            saved=True,
        )
    if has_inline:
        source = source_commands._source_definition(
            name="<inline>",
            orgs=orgs,
            teams=teams,
            repos=repos,
            paths=paths,
            ref_kinds=ref_kinds,
            ref_patterns=ref_patterns,
        )
        key = source_commands._inline_source_key(source)
        return _GraphSource(
            selections=(
                _GraphSourceSelection(
                    definition=source,
                    key=key,
                    label=f"inline source {key.removeprefix('inline:')}",
                ),
            ),
            key=key,
            label=f"inline source {key.removeprefix('inline:')}",
            saved=False,
        )
    return _GraphSource(selections=(), key=None, label=None, saved=False)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _graph_source_key(selections: list[_GraphSourceSelection]) -> str:
    if len(selections) == 1:
        return selections[0].key
    names = ",".join(selection.key.removeprefix("source:") for selection in selections)
    return f"sources:{names}"


def _graph_source_label(selections: list[_GraphSourceSelection]) -> str:
    if len(selections) == 1:
        return selections[0].label
    return f"sources {', '.join(selection.label for selection in selections)}"


def _dependency_index_for_graph_source(
    index: DependencyIndex,
    source_state: _GraphSource,
) -> DependencyIndex:
    if len(source_state.selections) <= 1:
        return index
    return MultiSourceDependencyIndex(
        index,
        tuple(selection.key for selection in source_state.selections),
    )


def _should_refresh_source(
    *,
    source_state: _GraphSource,
    direction: GraphDirection,
    cached: bool,
    live: bool,
    refresh: bool,
) -> bool:
    if refresh:
        return True
    if cached or not source_state.selections:
        return False
    return not (live and direction == "deps")


def _effective_direction(
    *,
    target: str,
    source_state: _GraphSource,
    index: SqliteDependencyIndex,
    direction: GraphDirection,
    cached: bool,
) -> tuple[GraphDirection, list[str]]:
    if not source_state.selections:
        if direction == "deps":
            return direction, []
        message = (
            "upstream requires --source NAME or inline selectors like --org, --team, or --repo"
        )
        if direction == "impact":
            raise UntapedError(message)
        return "deps", [
            "only showing downstream; upstream omitted because no source is configured. "
            "Pass --source NAME or inline selectors."
        ]
    missing = tuple(
        selection for selection in source_state.selections if index.status(selection.key) is None
    )
    if cached and source_state.saved and missing:
        raise UntapedError(_missing_source_index_message(target, source_state, missing))
    if direction == "deps":
        return direction, []
    if not missing:
        return direction, []
    message = _missing_source_index_message(target, source_state, missing)
    if direction == "impact":
        raise UntapedError(message)
    return "deps", [f"upstream omitted: {message}"]


def _missing_source_index_message(
    target: str,
    source_state: _GraphSource,
    missing: tuple[_GraphSourceSelection, ...],
) -> str:
    if source_state.saved:
        if len(missing) > 1:
            labels = ", ".join(repr(selection.label) for selection in missing)
            refresh_commands = " and ".join(
                f"`untaped ansible source refresh {selection.label}`" for selection in missing
            )
            source_flags = " ".join(
                f"--source {selection.label}" for selection in source_state.selections
            )
            return (
                f"no cached source data found for sources {labels}. Run: {refresh_commands}. "
                f"Or re-run graph with: "
                f"`untaped ansible graph {target} {source_flags} --upstream --refresh`."
            )
        label = missing[0].label
        return (
            f"no cached source data found for source {label!r}. Run: "
            f"`untaped ansible source refresh {label}`. Or re-run graph with: "
            f"`untaped ansible graph {target} --source {label} --upstream --refresh`."
        )
    return (
        "no cached source data found for inline source. Re-run this graph command with "
        "`--refresh` to scan GitHub and cache the result."
    )


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
    parser = ConfigParser()
    parser.read(git_config)
    origin_url = parser.get('remote "origin"', "url", fallback=None)
    if origin_url:
        declaration = DependencyDeclaration(
            name=origin_url,
            src=origin_url,
            source_path="<git-remote>",
        )
        return IdentityResolver().resolve(declaration).repo
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
    live: bool,
) -> bool:
    if direction == "impact":
        return False
    if source_key is None:
        return True
    return live


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
