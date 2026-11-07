"""A private repository's token reaches git through the environment, never
argv, the URL or an error message."""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from server.code.app.index import gitrepo, sandbox
from tests.unit.code_fixtures import git_config

HEADER = "http.https://github.com/o/.extraheader"


def test_auth_env_carries_the_token_as_an_extraheader() -> None:
    env = gitrepo.auth_env("ghp_secret123", "https://github.com/o/example-private.git")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    header = git_config(env)[HEADER]
    basic = base64.b64decode(header.split()[-1]).decode()
    assert basic == "x-access-token:ghp_secret123"
    assert "ghp_secret123" not in header  # encoded, not raw


@pytest.mark.parametrize(
    ("remote", "key"),
    [
        ("https://github.com/o/r.git", "http.https://github.com/o/.extraheader"),
        ("https://gitlab.com/group/sub/r.git", "http.https://gitlab.com/group/.extraheader"),
        ("https://git.example.com/r.git", "http.https://git.example.com/.extraheader"),
        ("https://github.com/o", "http.https://github.com/.extraheader"),
    ],
)
def test_the_token_is_sent_only_under_the_remote_owner(remote: str, key: str) -> None:
    """A submodule URL of another owner on the same host gets no credential."""
    headers = [k for k in git_config(gitrepo.auth_env("tok", remote)) if k.endswith(".extraheader")]
    assert headers == [key]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    ("url", "sent"),
    [
        ("https://github.com/o/sub.git", True),
        ("https://github.com/other/x.git", False),
        ("https://github.com/o-evil/x.git", False),
        ("https://gitlab.com/o/x.git", False),
    ],
)
def test_git_matches_the_header_to_the_remote_owner_only(
    tmp_path: Path, url: str, sent: bool
) -> None:
    """Git's own URL matching, not just the key string: the header reaches the
    remote owner's repositories and no other owner or host."""
    env = gitrepo.auth_env("tok", "https://github.com/o/r.git")
    got = subprocess.run(
        ["git", "config", "--get-urlmatch", "http.extraheader", url],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert got.stdout.startswith("AUTHORIZATION: basic ") is sent


def test_an_ssh_remote_gets_no_extraheader() -> None:
    env = gitrepo.auth_env("tok", "git@github.com:o/r.git")
    assert not any(k.endswith(".extraheader") for k in git_config(env))


def test_no_token_sets_no_extraheader() -> None:
    env = gitrepo.auth_env("")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(k.endswith(".extraheader") for k in git_config(env))


def test_git_reads_no_system_or_global_config_and_allows_only_https_and_ssh() -> None:
    env = gitrepo.auth_env("tok", "https://github.com/o/r.git")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    config = git_config(env)
    assert config["protocol.allow"] == "never"
    assert config["protocol.https.allow"] == "always"
    assert config["protocol.ssh.allow"] == "always"
    assert "protocol.file.allow" not in config  # file://, ext:: and the rest stay refused


def test_only_a_local_remote_allows_the_file_transport() -> None:
    assert git_config(gitrepo.auth_env("", "/srv/mirror.git"))["protocol.file.allow"] == "always"
    assert "protocol.file.allow" not in git_config(gitrepo.auth_env("", "git@github.com:o/r.git"))


@pytest.mark.parametrize(
    ("remote", "local"),
    [
        ("/srv/mirror.git", True),
        ("file:///srv/mirror.git", True),
        ("./mirror.git", True),
        ("mirror.git", True),  # git reads a bare relative path as local too
        ("mirrors/a.git", True),
        ("git@github.com:o/r.git", False),
        ("host:r.git", False),
        ("https://github.com/o/r.git", False),
        ("", False),  # no remote: a submodule update must not get the file transport
    ],
)
def test_a_remote_is_local_exactly_when_git_reads_it_as_a_path(remote: str, local: bool) -> None:
    assert gitrepo.is_local_remote(remote) is local


def test_git_inherits_no_server_environment(monkeypatch) -> None:
    monkeypatch.setenv("LOOM_JWT_SECRET", "server-secret")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    env = gitrepo.auth_env("tok", "https://github.com/o/r.git")
    assert "LOOM_JWT_SECRET" not in env and "GIT_SSH_COMMAND" not in env
    assert set(env) - {"PATH", "HOME", "LANG", "TMPDIR"} <= {k for k in env if k.startswith("GIT_")}


def test_remote_port_names_the_port_the_egress_proxy_must_allow() -> None:
    assert gitrepo.remote_port("https://github.com/o/r.git") == 443
    assert gitrepo.remote_port("https://git.example.com:8443/o/r.git") == 8443
    assert gitrepo.remote_port("ssh://git@example.com:2222/o/r.git") == 2222
    assert gitrepo.remote_port("git@github.com:o/r.git") == 22
    assert gitrepo.remote_port("/srv/mirror.git") is None


def test_fetch_passes_the_token_to_the_environment_not_argv(monkeypatch) -> None:
    seen = {}

    async def _job(argv, **kw):
        seen["argv"] = tuple(argv)
        seen["env"] = kw.get("env") or {}
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(sandbox, "run_job", _job)
    asyncio.run(
        gitrepo.fetch("/tmp/x.git", "https://github.com/o/r.git", "master", token="tok_abc")
    )
    assert "tok_abc" not in " ".join(seen["argv"])
    assert HEADER in git_config(seen["env"])
    argv = seen["argv"]
    assert argv[0] == "git"
    assert argv[argv.index("--git-dir") :][:3] == ("--git-dir", "/tmp/x.git", "fetch")


def test_git_error_text_never_carries_the_token(monkeypatch) -> None:
    async def _job(argv, **kw):
        err = b"fatal: could not read from https://x-access-token:tok_abc@github.com/o/r.git"
        return sandbox.JobResult(128, b"", err)

    monkeypatch.setattr(sandbox, "run_job", _job)
    with pytest.raises(gitrepo.GitError) as ei:
        asyncio.run(
            gitrepo.fetch("/tmp/x.git", "https://github.com/o/r.git", "master", token="tok_abc")
        )
    assert "tok_abc" not in str(ei.value) and "<token>" in str(ei.value)
