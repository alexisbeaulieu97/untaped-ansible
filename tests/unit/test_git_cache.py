"""Tests for the local bare Git cache adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from untaped_ansible.infrastructure.git_cache import GitCacheError, GitRepositoryCache


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
