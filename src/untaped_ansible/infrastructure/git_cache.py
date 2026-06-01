"""Bare Git cache used by dependency source indexing."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from untaped_ansible.application.ports import GitRef

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
        self._run(
            ["remote", "remove", "origin"],
            cwd=bare,
            check=False,
            auth_header=auth_header,
        )
        self._run(["remote", "add", "origin", url], cwd=bare, auth_header=auth_header)
        return bare

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
        try:
            self._run(args, cwd=bare_path, timeout=self._slow_timeout, auth_header=auth_header)
        except GitCacheError as exc:
            if "couldn't find remote ref" in str(exc):
                return
            raise

    def list_refs(self, bare_path: Path, kind: str) -> list[GitRef]:
        """List locally fetched refs under ``refs/<kind>``."""
        prefix = f"refs/{kind}/"
        out = self._run(
            ["for-each-ref", "--format=%(refname) %(objectname)", f"refs/{kind}"],
            cwd=bare_path,
            capture=True,
        )
        refs: list[GitRef] = []
        for line in out.splitlines():
            full_ref, _, sha = line.partition(" ")
            if not full_ref.startswith(prefix) or not sha:
                continue
            refs.append(GitRef(kind=kind, name=full_ref.removeprefix(prefix), sha=sha))
        return sorted(refs, key=lambda ref: (ref.kind, ref.name, ref.sha))

    def read_file(self, bare_path: Path, sha: str, path: str) -> str | None:
        """Read ``path`` from ``sha`` without checking out a worktree."""
        try:
            return self._run(["show", f"{sha}:{path}"], cwd=bare_path, capture=True)
        except GitCacheError:
            return None

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
        cmd = [self._git_path]
        display_args = list(args)
        if auth_header is not None:
            cmd.extend(["-c", f"http.extraheader={auth_header}"])
            display_args = ["-c", "http.extraheader=<redacted>", *display_args]
        cmd.extend(args)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCacheError(
                f"git {' '.join(display_args)} timed out after {effective_timeout:g}s"
            ) from exc
        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
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
