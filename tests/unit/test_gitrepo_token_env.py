"""A private repository's token reaches git through the environment, never
argv, the URL or an error message."""

from __future__ import annotations

import asyncio
import base64

import pytest

from server.code.app.index import gitrepo


def test_auth_env_carries_the_token_as_an_extraheader() -> None:
    env = gitrepo.auth_env("ghp_secret123", "https://github.com/ClickHouse/example-private.git")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    basic = base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode()
    assert basic == "x-access-token:ghp_secret123"
    assert "ghp_secret123" not in env["GIT_CONFIG_VALUE_0"]  # encoded, not raw


def test_no_token_sets_no_config() -> None:
    env = gitrepo.auth_env("")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in env and "GIT_CONFIG_VALUE_0" not in env


def test_fetch_passes_the_token_to_the_environment_not_argv(monkeypatch) -> None:
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _exec(*argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env") or {}
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    asyncio.run(
        gitrepo.fetch("/tmp/x.git", "https://github.com/o/r.git", "master", token="tok_abc")
    )
    assert "tok_abc" not in " ".join(seen["argv"])
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    argv = seen["argv"]
    assert argv[0] == "git"
    assert argv[argv.index("--git-dir") :][:3] == ("--git-dir", "/tmp/x.git", "fetch")


def test_git_error_text_never_carries_the_token(monkeypatch) -> None:
    class _Proc:
        returncode = 128

        async def communicate(self):
            return (
                b"",
                b"fatal: could not read from https://x-access-token:tok_abc@github.com/o/r.git",
            )

    async def _exec(*argv, **kw):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(gitrepo.GitError) as ei:
        asyncio.run(
            gitrepo.fetch("/tmp/x.git", "https://github.com/o/r.git", "master", token="tok_abc")
        )
    assert "tok_abc" not in str(ei.value) and "<token>" in str(ei.value)
