"""Network git through an operator's forward proxy (LOOM_CODE_GIT_PROXY_URL).

With the setting, every repository job that is git talking to a network remote (a
mirror's clone, fetch and ls-remote, the ls-remote before an add, a checkout's submodule
update) connects through that proxy as an identity: the repository entry's
`git_proxy_identity`, else LOOM_CODE_GIT_PROXY_IDENTITY. The proxy decides by the
identity what the job may reach and with which credential.

`sandbox.run_job` calls `job_env` for a job that carries `NetworkGit`, on its
plain-process branch only. The proxy's password is read from its file for that one job
and lives in that one child's environment: it reaches no argv, no stored git config, no
`auth_env` (which build harnesses copy) and no sandboxed job, and `redact` takes it out
of what git says before anything is logged or recorded. A job that cannot go through
the proxy is refused (`GitProxyRefused`): git never connects directly while the setting
is set. A job the proxy itself refuses fails as git fails; `refused_by_proxy` names the
proxy for the callers that show a fixed reason in place of git's words.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from server.code.domain.index import tier_health
from server.code.domain.index.state import (
    GIT_PROXY_IDENTITY_REFUSAL,
    GIT_PROXY_REFUSED,
    GIT_PROXY_REMOTE_REFUSAL,
    GIT_PROXY_TOKEN_REFUSAL,
)
from server.settings import settings

# What a child's output shows in place of the proxy password.
REDACTED = b"<proxy-token>"


@dataclass(frozen=True)
class NetworkGit:
    """Marks a repository job as git talking to a network remote
    (`sandbox.JobOptions.network_git`). `remote` is the remote the job names, '' when it
    names none (a submodule update follows .gitmodules); `identity` is the entry's
    `git_proxy_identity`, '' for LOOM_CODE_GIT_PROXY_IDENTITY."""

    remote: str = ""
    identity: str = ""


class GitProxyRefused(RuntimeError):
    """A network git job refused while LOOM_CODE_GIT_PROXY_URL is set. `reason` is one of
    `state.GIT_PROXY_REFUSALS`: a sentence Loom wrote, which a caller may show as it is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def configured() -> bool:
    """Whether network git goes through an operator's proxy on this deployment."""
    return bool(settings.code_git_proxy_url)


async def job_env(
    env: Mapping[str, str], git: NetworkGit
) -> tuple[dict[str, str], tuple[bytes, ...]]:
    """(the child's environment, the spellings of the password to redact from its
    output) for a network git job that runs as a plain process while the proxy is set.
    A copy: `env` is the caller's and never holds the password."""
    from server.code.app.index.gitrepo import add_git_config

    if git.remote and not git.remote.startswith("https://"):
        raise GitProxyRefused(GIT_PROXY_REMOTE_REFUSAL)
    identity = git.identity or settings.code_git_proxy_identity
    if not identity:
        raise GitProxyRefused(GIT_PROXY_IDENTITY_REFUSAL)
    token = await asyncio.to_thread(_read_token)
    out = dict(env)
    add_git_config(out, "http.proxy", _proxy_url(identity, token))
    # Without it git's HTTP library sends the first CONNECT bare and waits for a 407.
    add_git_config(out, "http.proxyAuthMethod", "basic")
    if settings.code_git_proxy_ca_file:
        add_git_config(out, "http.sslCAInfo", settings.code_git_proxy_ca_file)
    if settings.code_git_proxy_tls_ca_file:
        # For an https:// proxy's own certificate: http.sslCAInfo is for the remote's.
        add_git_config(out, "http.proxySSLCAInfo", settings.code_git_proxy_tls_ca_file)
    # ssh has no HTTP proxy: a submodule or redirect that names it would connect directly.
    add_git_config(out, "protocol.ssh.allow", "never")
    return out, _secret_forms(identity, token)


def redact(output: bytes, secrets: Sequence[bytes]) -> bytes:
    """`output` with every spelling in `secrets` replaced."""
    for secret in secrets:
        output = output.replace(secret, REDACTED)
    return output


def refused_by_proxy(git_output: str) -> str:
    """What a caller that may not show git's own words says of a job the proxy refused:
    `state.GIT_PROXY_REFUSED` with the status the proxy answered git's CONNECT with. ''
    when the proxy is not set or `git_output` names no such answer."""
    status = tier_health.refused_connect_status(git_output) if configured() else ""
    return f"{GIT_PROXY_REFUSED} ({status})" if status else ""


def _read_token() -> str:
    """The proxy password as its file holds it now, '' when no file is named. Read for
    every job, since the platform rotates it. GitProxyRefused for a named file that
    gives no token: the job must not run without the password it was given."""
    path = settings.code_git_proxy_token_file
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except (OSError, UnicodeDecodeError):
        token = ""
    if not token:
        raise GitProxyRefused(GIT_PROXY_TOKEN_REFUSAL)
    return token


def _proxy_url(identity: str, token: str) -> str:
    """LOOM_CODE_GIT_PROXY_URL with the user and password git hands its HTTP library,
    each percent-encoded whole, so no character of either ends the URL's userinfo. The
    colon stays with an empty password: without one git asks a credential helper."""
    parts = urlsplit(settings.code_git_proxy_url)
    return f"{parts.scheme}://{quote(identity, safe='')}:{quote(token, safe='')}@{parts.netloc}"


def _secret_forms(identity: str, token: str) -> tuple[bytes, ...]:
    """The spellings of the password a git child could print: as its file holds it,
    percent-encoded as in `http.proxy`, and inside the Basic pair of the
    Proxy-Authorization header. Longest first, so no spelling is cut by a shorter one."""
    if not token:
        return ()
    basic = base64.b64encode(f"{identity}:{token}".encode()).decode()
    forms = {token, quote(token, safe=""), basic}
    return tuple(sorted((form.encode() for form in forms), key=len, reverse=True))


__all__ = [
    "REDACTED",
    "GitProxyRefused",
    "NetworkGit",
    "configured",
    "job_env",
    "redact",
    "refused_by_proxy",
]
