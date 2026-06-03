"""Bare Git cache used by dependency source indexing."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from untaped_ansible.domain.payloads import GitRef

DEFAULT_TIMEOUT = 60.0
DEFAULT_SLOW_TIMEOUT = 600.0


class GitCacheError(RuntimeError):
    """Raised when local Git cache operations fail."""


class GitRepositoryCache:
    """Maintain bare repositories and read dependency files from Git objects."""

    def __init__(
        self,
        *,
        git: str = "git",
        timeout: float = DEFAULT_TIMEOUT,
        slow_timeout: float = DEFAULT_SLOW_TIMEOUT,
    ) -> None:
        self._git = git
        self._git_path = shutil.which(git)
        self._timeout = timeout
        self._slow_timeout = slow_timeout

    def ensure_bare(
        self,
        url: str,
        *,
        cache_dir: Path,
        auth_header: str | None,
    ) -> Path:
        """Ensure a bare repository cache exists for ``url``."""
        bare = cache_path_for(url, cache_dir=cache_dir)
        if not (bare / "HEAD").is_file():
            bare.parent.mkdir(parents=True, exist_ok=True)
            self._run(["init", "--bare", str(bare)], timeout=self._slow_timeout)
        self._ensure_origin(bare, url, auth_header=auth_header)
        return bare

    def _ensure_origin(self, bare: Path, url: str, *, auth_header: str | None) -> None:
        current_url = self._run(
            ["remote", "get-url", "origin"],
            cwd=bare,
            capture=True,
            check=False,
            auth_header=auth_header,
        ).strip()
        if not current_url:
            self._run(["remote", "add", "origin", url], cwd=bare, auth_header=auth_header)
            return
        if current_url != url:
            self._run(["remote", "set-url", "origin", url], cwd=bare, auth_header=auth_header)

    def fetch_refs(
        self,
        bare_path: Path,
        *,
        refspecs: list[str],
        depth: int,
        blob_filter: bool,
        auth_header: str | None,
    ) -> None:
        """Fetch selected refs into a bare cache."""
        if not refspecs:
            return
        args = ["fetch", "--prune", "origin"]
        if depth > 0:
            args.append(f"--depth={depth}")
        if blob_filter:
            args.append("--filter=blob:none")
        args.extend(refspecs)
        self._run(args, cwd=bare_path, timeout=self._slow_timeout, auth_header=auth_header)

    def list_remote_refs(
        self,
        url: str,
        *,
        namespaces: list[str],
        auth_header: str | None,
    ) -> list[GitRef]:
        """List refs from ``url`` without updating the local bare cache."""
        if not namespaces:
            return []
        patterns = [
            pattern for namespace in namespaces for pattern in _remote_ref_patterns(namespace)
        ]
        out = self._run(
            ["ls-remote", url, *patterns],
            capture=True,
            timeout=self._slow_timeout,
            auth_header=auth_header,
        )
        refs: dict[tuple[str, str], GitRef] = {}
        peeled_tags: dict[tuple[str, str], str] = {}
        for line in out.splitlines():
            sha, full_ref = _split_remote_ref_line(line)
            if sha is None or full_ref is None:
                continue
            peeled = full_ref.endswith("^{}")
            if peeled:
                full_ref = full_ref.removesuffix("^{}")
            kind, name = _kind_and_name(full_ref)
            if kind is None or name is None:
                continue
            key = (kind, name)
            if peeled:
                peeled_tags[key] = sha
                continue
            refs[key] = GitRef(kind=kind, name=name, sha=sha)
        for key, sha in peeled_tags.items():
            if key in refs:
                refs[key] = refs[key].model_copy(update={"sha": sha})
        return [refs[key] for key in sorted(refs)]

    def list_refs(self, bare_path: Path, kind: str) -> list[GitRef]:
        """List locally fetched refs under ``refs/<kind>``."""
        prefix = f"refs/{kind}/"
        out = self._run(
            [
                "for-each-ref",
                "--format=%(refname) %(objecttype) %(objectname) %(*objectname)",
                f"refs/{kind}",
            ],
            cwd=bare_path,
            capture=True,
        )
        refs: list[GitRef] = []
        for line in out.splitlines():
            full_ref, object_type, object_sha, peeled_sha = _split_ref_line(line)
            sha = peeled_sha if object_type == "tag" and peeled_sha else object_sha
            if not full_ref.startswith(prefix) or not sha:
                continue
            refs.append(GitRef(kind=kind, name=full_ref.removeprefix(prefix), sha=sha))
        return sorted(refs, key=lambda ref: (ref.kind, ref.name, ref.sha))

    def read_file(
        self,
        bare_path: Path,
        sha: str,
        path: str,
        *,
        auth_header: str | None,
    ) -> str | None:
        """Read ``path`` from ``sha`` without checking out a worktree."""
        try:
            return self._run(
                ["show", f"{sha}:{path}"],
                cwd=bare_path,
                capture=True,
                auth_header=auth_header,
            )
        except GitCacheError as exc:
            if _is_missing_path_error(str(exc)):
                return None
            raise

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
        timeout: float | None = None,
        auth_header: str | None = None,
    ) -> str:
        if self._git_path is None:
            raise GitCacheError(f"`{self._git}` not found on PATH")
        effective_timeout = self._timeout if timeout is None else timeout
        cmd = [self._git_path, *args]
        display_args = list(args)
        env = None
        auth_config_path: Path | None = None
        if auth_header is not None:
            env, auth_config_path = _auth_config_env(auth_header)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCacheError(
                f"git {' '.join(display_args)} timed out after {effective_timeout:g}s"
            ) from exc
        finally:
            if auth_config_path is not None:
                auth_config_path.unlink(missing_ok=True)
        if check and result.returncode != 0:
            stderr = _redact((result.stderr or "").strip(), auth_header)
            raise GitCacheError(f"git {' '.join(display_args)} failed: {stderr or 'no stderr'}")
        return result.stdout if capture else ""


def cache_path_for(url: str, *, cache_dir: Path) -> Path:
    """Return the deterministic bare-cache path for a remote URL."""
    parsed = urlparse(url)
    if parsed.scheme and parsed.path:
        base_name = Path(parsed.path.rstrip("/")).name
        host = parsed.netloc or "local"
    elif ":" in url and "@" in url.split(":", maxsplit=1)[0]:
        host_part, _, path_part = url.partition(":")
        host = host_part.rsplit("@", maxsplit=1)[-1]
        base_name = Path(path_part.rstrip("/")).name
    else:
        host = "local"
        base_name = Path(url.rstrip("/")).name
    if not base_name:
        base_name = "repository"
    if not base_name.endswith(".git"):
        base_name = f"{base_name}.git"
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    safe_host = _safe_path_part(host)
    safe_name = _safe_path_part(base_name.removesuffix(".git"))
    return cache_dir.expanduser() / safe_host / f"{safe_name}-{digest}.git"


def _safe_path_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def _split_ref_line(line: str) -> tuple[str, str, str, str]:
    parts = line.split(" ", maxsplit=3)
    while len(parts) < 4:
        parts.append("")
    return parts[0], parts[1], parts[2], parts[3]


def _split_remote_ref_line(line: str) -> tuple[str | None, str | None]:
    sha, separator, ref = line.partition("\t")
    if not separator or not sha or not ref:
        return None, None
    return sha, ref


def _kind_and_name(full_ref: str) -> tuple[str | None, str | None]:
    for kind in ("heads", "tags"):
        prefix = f"refs/{kind}/"
        if full_ref.startswith(prefix):
            return kind, full_ref.removeprefix(prefix)
    return None, None


def _remote_ref_patterns(namespace: str) -> tuple[str, ...]:
    kind, _, suffix = namespace.partition("/")
    if not suffix:
        return (f"refs/{kind}/*",)
    if suffix.endswith("/"):
        return (f"refs/{kind}/{suffix}*",)
    pattern = f"refs/{kind}/{suffix}"
    if kind == "tags" and not _has_ref_wildcard(pattern):
        return (pattern, f"{pattern}^{{}}")
    return (pattern,)


def _has_ref_wildcard(pattern: str) -> bool:
    return any(token in pattern for token in ("*", "?", "["))


def _auth_config_env(auth_header: str) -> tuple[dict[str, str], Path]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="untaped-git-auth-",
        suffix=".config",
        delete=False,
    ) as auth_config:
        auth_config.write("[http]\n")
        auth_config.write(f"\textraheader = {auth_header}\n")
        path = Path(auth_config.name)
    env = os.environ.copy()
    count = _git_config_count(env)
    env[f"GIT_CONFIG_KEY_{count}"] = "include.path"
    env[f"GIT_CONFIG_VALUE_{count}"] = str(path)
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env, path


def _git_config_count(env: dict[str, str]) -> int:
    raw = env.get("GIT_CONFIG_COUNT")
    if raw is None:
        return 0
    try:
        count = int(raw)
    except ValueError:
        return 0
    return max(count, 0)


def _redact(value: str, secret: str | None) -> str:
    if secret is None:
        return value
    return value.replace(secret, "<redacted>")


def _is_missing_path_error(message: str) -> bool:
    return "does not exist in" in message or "exists on disk, but not in" in message
