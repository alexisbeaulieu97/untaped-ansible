"""Graph command and graph-specific CLI helpers for the Ansible plugin."""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Group, Parameter, validators
from untaped.api import (
    ProfileOverrideOption,
    UntapedError,
    echo,
    plugin_context,
    raise_usage,
    report_errors,
)
from untaped_github import GithubClient, GithubSettings

import untaped_ansible.cli.source_commands as source_commands
from untaped_ansible.application import BuildGraph, GraphRequest
from untaped_ansible.application.ports import DependencyIndex
from untaped_ansible.cli._refresh import pluralize, run_source_refresh
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
GraphFormatOption = Annotated[
    GraphFormat,
    Parameter(name=["--format", "-f"], help="Graph output format."),
]

# LimitedChoice() defaults to at-most-one selection — cyclopts' MutuallyExclusive
# is an untyped alias for exactly this, so the typed parent is used directly.
_DIRECTION_GROUP = Group("Direction", validator=validators.LimitedChoice())
_SOURCE_DATA_GROUP = Group("Source Data", validator=validators.LimitedChoice())


def register_graph_command(app: App) -> None:
    """Register graph commands on the Ansible root app."""
    app.command(graph_command, name="graph", help=_GRAPH_HELP)


_GRAPH_HELP = (
    "Graph Ansible dependency relationships for a role, repo, or playbook. "
    "Inline source selectors (--org, --team, --repo, --path, --ref-kind, "
    "--ref-pattern) are cached under a deterministic fingerprint key, so "
    "repeated identical invocations reuse the same scan. "
    "Examples: untaped ansible graph acme/base --org acme --team platform "
    "--upstream --refresh; untaped ansible graph acme/app --source prod "
    "--both --cached; untaped ansible graph ./roles/web --target-repo acme/web "
    "--downstream."
)


def graph_command(
    target: Annotated[
        str,
        Parameter(help="Target repo, GitHub URL, alias, or local path."),
    ],
    /,
    *,
    ref: Annotated[
        str | None,
        Parameter(
            name="--ref",
            help="Target branch, tag, or SHA for live dependency reads and cached upstream lookup.",
        ),
    ] = None,
    source: Annotated[
        list[str] | None,
        Parameter(
            name="--source",
            help="Saved source to use for cached graph data and upstream impact; repeat to union.",
            consume_multiple=False,
        ),
    ] = None,
    upstream: Annotated[
        bool,
        Parameter(
            name="--upstream",
            negative="",
            group=_DIRECTION_GROUP,
            help="Show repos that depend on TARGET (reverse impact; requires a source).",
        ),
    ] = False,
    downstream: Annotated[
        bool,
        Parameter(
            name="--downstream",
            negative="",
            group=_DIRECTION_GROUP,
            help="Show what TARGET depends on (works without a source).",
        ),
    ] = False,
    both: Annotated[
        bool,
        Parameter(
            name="--both",
            negative="",
            group=_DIRECTION_GROUP,
            help="Show upstream and downstream (default). Upstream still requires a source.",
        ),
    ] = False,
    refresh: Annotated[
        bool,
        Parameter(
            name="--refresh",
            negative="",
            group=_SOURCE_DATA_GROUP,
            help="Refresh source data before graphing.",
        ),
    ] = False,
    cached: Annotated[
        bool,
        Parameter(
            name="--cached",
            negative="",
            group=_SOURCE_DATA_GROUP,
            help="Use cached source data without checking remote refs.",
        ),
    ] = False,
    concurrency: Annotated[
        int | None,
        Parameter(
            name="--concurrency",
            validator=validators.Number(gte=1, lte=32),
            help=(
                "Git-backed source refresh concurrency; defaults to ansible.git_fetch_concurrency."
            ),
        ),
    ] = None,
    live: Annotated[
        bool,
        Parameter(
            name="--live",
            negative="",
            group=_SOURCE_DATA_GROUP,
            help=(
                "Use live GitHub reads for downstream graphing even when source data is configured."
            ),
        ),
    ] = False,
    depth: Annotated[str, Parameter(name="--depth", help="Traversal depth or 'unlimited'.")] = "3",
    target_repo: Annotated[
        str | None,
        Parameter(name="--target-repo", help="Canonical owner/repo override for local targets."),
    ] = None,
    orgs: Annotated[
        list[str] | None,
        Parameter(name="--org", help="Inline source GitHub org.", consume_multiple=False),
    ] = None,
    teams: Annotated[
        list[str] | None,
        Parameter(
            name="--team",
            help=(
                "Inline source GitHub team as ORG/SLUG; a bare SLUG is allowed when "
                "exactly one --org is given and normalizes to ORG/SLUG."
            ),
            consume_multiple=False,
        ),
    ] = None,
    source_repos: Annotated[
        list[str] | None,
        Parameter(
            name="--repo",
            help="Inline source GitHub repo as owner/name.",
            consume_multiple=False,
        ),
    ] = None,
    paths: Annotated[
        list[str] | None,
        Parameter(name="--path", help="Inline source dependency path.", consume_multiple=False),
    ] = None,
    ref_kinds: Annotated[
        list[str] | None,
        Parameter(
            name="--ref-kind",
            help="Inline source ref namespace to scan: heads or tags; omit for configured default.",
            consume_multiple=False,
        ),
    ] = None,
    ref_patterns: Annotated[
        list[str] | None,
        Parameter(
            name="--ref-pattern",
            help="Inline source fnmatch pattern for branch/tag names; omit for configured default.",
            consume_multiple=False,
        ),
    ] = None,
    fmt: GraphFormatOption = "tree",
    output: Annotated[
        Path | None,
        Parameter(name="--output", help="Write graph data to a file."),
    ] = None,
    profile: ProfileOverrideOption = None,
) -> None:
    """Graph Ansible dependency relationships for a role, repo, or playbook.

    Examples:
      untaped ansible graph acme/base --org acme --team platform --upstream --refresh
      untaped ansible graph acme/app --source prod --both --cached
      untaped ansible graph ./roles/web --target-repo acme/web --downstream
    """
    if refresh and not any((source, orgs, teams, source_repos, paths, ref_kinds, ref_patterns)):
        raise_usage("--refresh requires --source or inline source selectors")
    with report_errors():
        ctx = plugin_context(profile)
        settings = ctx.section("ansible", AnsibleSettings)
        aliases = AliasRepository().entries()
        target_repo_name = target_repo or _resolve_target_repo(target, aliases)
        if target_repo_name is None:
            raise UntapedError(f"could not resolve target to a GitHub repo: {target!r}")

        direction = _graph_direction(upstream=upstream, downstream=downstream, both=both)
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
        refresh_warnings: list[str] = []
        if should_refresh_source:
            github_settings = ctx.section("github", GithubSettings)
            for selection in graph_source.selections:
                if not refresh and _within_freshness_ttl(
                    sqlite_index,
                    selection,
                    ttl=settings.freshness_ttl,
                ):
                    continue
                result = run_source_refresh(
                    selection.definition,
                    source_key=selection.key,
                    action="refreshed" if refresh else "checked",
                    label=selection.label,
                    index=sqlite_index,
                    aliases=aliases,
                    settings=settings,
                    github_settings=github_settings,
                    http=ctx.http,
                    concurrency=git_concurrency,
                )
                if result.failures:
                    refresh_warnings.append(
                        f"refresh of {selection.label} had "
                        f"{pluralize(len(result.failures), 'failure')}; "
                        "data for those repos may be stale"
                    )

        direction, graph_warnings = _effective_direction(
            target=target,
            source_state=graph_source,
            index=sqlite_index,
            direction=direction,
            cached=cached,
        )
        refresh_hint = _refresh_hint(graph_source)

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
                refresh_hint=refresh_hint,
            )
        else:
            if _should_use_live_dependencies(
                direction=direction,
                source_key=graph_source.key,
                live=live,
            ):
                github_settings = ctx.section("github", GithubSettings)
                with GithubClient(github_settings, http=ctx.http) as github:
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
                        refresh_hint=refresh_hint,
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
                    refresh_hint=refresh_hint,
                )

        graph = _with_graph_warnings(
            graph,
            [
                *refresh_warnings,
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
    # Mutual exclusion is enforced at parse time by _DIRECTION_GROUP.
    del both
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
        raise_usage(
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
    refresh_hint: str | None,
) -> DependencyGraph:
    return BuildGraph(index)(
        GraphRequest(
            repo=repo,
            ref=ref,
            source_key=source_key,
            direction=direction,
            depth=_parse_depth(depth),
            stale_after=stale_after,
            refresh_hint=refresh_hint,
        )
    )


def _refresh_hint(source_state: _GraphSource) -> str | None:
    """Compose the exact fix command surfaced in stale/missing-ref warnings."""
    if not source_state.selections:
        return None
    if source_state.saved:
        commands = " and ".join(
            f"`untaped ansible source refresh {selection.label}`"
            for selection in source_state.selections
        )
        return f"Run {commands} to update it."
    return "Re-run this graph command with `--refresh` to update it."


def _within_freshness_ttl(
    index: SqliteDependencyIndex,
    selection: _GraphSourceSelection,
    *,
    ttl: int | None,
) -> bool:
    """True (with one stderr info line) when the selection's scan is within the TTL."""
    if ttl is None:
        return False
    status = index.status(selection.key)
    if status is None:
        return False
    age = datetime.now(UTC) - status.scanned_at
    if age.total_seconds() > ttl:
        return False
    echo(
        f"source '{selection.label}' refreshed {_human_age(age)} ago "
        "(within freshness_ttl); skipping check — pass --refresh to force",
        err=True,
    )
    return True


def _human_age(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    if seconds < 60:
        return pluralize(seconds, "second")
    minutes = seconds // 60
    if minutes < 60:
        return pluralize(minutes, "minute")
    hours = minutes // 60
    if hours < 24:
        return pluralize(hours, "hour")
    return pluralize(hours // 24, "day")


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
        echo(rendered)
        return
    output.expanduser().parent.mkdir(parents=True, exist_ok=True)
    output.expanduser().write_text(rendered)


def _parse_depth(value: str) -> int | None:
    if value == "unlimited":
        return None
    try:
        depth = int(value)
    except ValueError:
        raise_usage("--depth must be an integer or 'unlimited'")
    if depth < 0:
        raise_usage("--depth must be >= 0")
    return depth
