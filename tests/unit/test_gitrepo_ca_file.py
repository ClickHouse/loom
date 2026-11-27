"""A remote under a private certificate authority: repository-job git reads no
system or global config, so the bundle that trusts it reaches git through the
job's environment."""

from __future__ import annotations

import asyncio

import pytest

from server.code.app.index import git_proxy, gitrepo
from server.settings import settings
from tests.unit.code_fixtures import git_config


def test_no_bundle_leaves_git_its_default_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_git_ca_file", "")
    env = gitrepo.auth_env("", "https://git.example.com/acme/widgets.git")
    assert "http.sslCAInfo" not in git_config(env)


def test_the_bundle_reaches_every_repository_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_git_ca_file", "/etc/ssl/private-ca/bundle.pem")
    for env in (
        gitrepo.auth_env(),
        gitrepo.auth_env("tok", "https://git.example.com/acme/widgets.git"),
    ):
        assert git_config(env)["http.sslCAInfo"] == "/etc/ssl/private-ca/bundle.pem"


async def _proxy_env(env: dict[str, str]) -> dict[str, str]:
    out, _ = await git_proxy.job_env(
        env, git_proxy.NetworkGit(remote="https://git.example.com/a/b.git", identity="loom")
    )
    return out


@pytest.mark.parametrize(
    ("proxy_ca", "want"),
    [("/etc/ssl/proxy-ca.pem", "/etc/ssl/proxy-ca.pem"), ("", "/etc/ssl/private-ca/bundle.pem")],
)
def test_through_the_proxy_its_bundle_wins_when_set(
    monkeypatch: pytest.MonkeyPatch, proxy_ca: str, want: str
) -> None:
    monkeypatch.setattr(settings, "code_git_ca_file", "/etc/ssl/private-ca/bundle.pem")
    monkeypatch.setattr(settings, "code_git_proxy_url", "http://proxy.example.com:3128")
    monkeypatch.setattr(settings, "code_git_proxy_ca_file", proxy_ca)
    monkeypatch.setattr(settings, "code_git_proxy_token_file", "")
    out = asyncio.run(_proxy_env(gitrepo.auth_env()))
    # git keeps the last value of a single-valued key
    assert git_config(out)["http.sslCAInfo"] == want
