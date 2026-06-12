"""Tests for the local bare Git cache adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from untaped_ansible.infrastructure.git_cache import (
    GitCacheError,
    GitRepositoryCache,
    cache_path_for,
)


def test_existing_bare_cache_updates_origin_without_remove_add_churn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bare = cache_path_for("https://github.com/acme/site.git", cache_dir=tmp_path / "cache")
    bare.mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/main\n")
    commands: list[list[str]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        assert isinstance(cmd, list)
        commands.append(cmd[1:])
        if cmd[1:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="old-url\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = GitRepositoryCache().ensure_bare(
        "https://github.com/acme/site.git",
        cache_dir=tmp_path / "cache",
        auth_header=None,
    )

    assert result == bare
    assert ["remote", "remove", "origin"] not in commands
    assert ["remote", "add", "origin", "https://github.com/acme/site.git"] not in commands
    assert ["remote", "set-url", "origin", "https://github.com/acme/site.git"] in commands


def test_auth_header_is_not_passed_in_git_argv(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        assert isinstance(cmd, list)
        env = kwargs.get("env")
        assert isinstance(env, dict)
        auth_config_path = Path(env["GIT_CONFIG_VALUE_0"])
        captured["cmd"] = cmd
        captured["env"] = env
        captured["auth_config_path"] = auth_config_path
        captured["auth_config"] = auth_config_path.read_text()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    GitRepositoryCache().fetch_refs(
        tmp_path,
        refspecs=["+refs/heads/main:refs/heads/main"],
        depth=1,
        blob_filter=True,
        auth_header="AUTHORIZATION: bearer secret-token",
    )

    assert "secret-token" not in " ".join(captured["cmd"])
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "include.path"
    assert "secret-token" not in "\n".join(
        value for key, value in captured["env"].items() if key.startswith("GIT_CONFIG_")
    )
    assert "AUTHORIZATION: bearer secret-token" in captured["auth_config"]
    assert not captured["auth_config_path"].exists()


def test_fetch_refs_propagates_missing_remote_ref(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        return subprocess.CompletedProcess(
            cmd,
            128,
            stdout="",
            stderr="fatal: couldn't find remote ref refs/heads/missing",
        )

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(GitCacheError, match="couldn't find remote ref"):
        GitRepositoryCache().fetch_refs(
            tmp_path,
            refspecs=["+refs/heads/missing:refs/heads/missing"],
            depth=1,
            blob_filter=True,
            auth_header=None,
        )


def test_read_file_returns_none_only_for_missing_paths(monkeypatch, tmp_path: Path) -> None:
    responses = [
        subprocess.CompletedProcess(
            ["git"],
            128,
            stdout="",
            stderr="fatal: path 'roles/requirements.yml' does not exist in 'abc123'",
        ),
        subprocess.CompletedProcess(
            ["git"],
            128,
            stdout="",
            stderr="fatal: unable to access 'https://github.com/acme/private.git/': denied",
        ),
    ]

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return responses.pop(0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("subprocess.run", fake_run)

    cache = GitRepositoryCache()

    assert (
        cache.read_file(
            tmp_path,
            "abc123",
            "roles/requirements.yml",
            auth_header=None,
        )
        is None
    )
    with pytest.raises(GitCacheError, match="unable to access"):
        cache.read_file(
            tmp_path,
            "abc123",
            "roles/requirements.yml",
            auth_header=None,
        )
