"""What git itself does with the proxy settings Loom hands it, run with the real binary
against a forward proxy the test starts: the first request carries the identity and the
password, the repository's own token stays inside the tunnel, the remote is trusted by
the CA bundle alone, an https proxy by a bundle of its own, a submodule goes the same
way, and nothing is stored."""

from __future__ import annotations

import asyncio
import base64
import datetime
import ipaddress
import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from server.code.app.index import bootstrap, compiler_tier, git_proxy, gitrepo, sandbox
from server.code.app.index import repo_registry as reg
from server.code.app.index.git_proxy import NetworkGit
from server.code.app.index.tier_health import ToolOutputError
from server.code.domain.index import tier_health
from server.code.domain.index.state import GIT_PROXY_REFUSED
from server.settings import settings
from tests.unit.code_fixtures import repo_config
from tests.unit.git_repo import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

IDENTITY = "acme.widgets-indexer"
TOKEN = "tok/en+with:odd chars%@"
ENCODED = quote(TOKEN, safe="")
BASIC = base64.b64encode(f"{IDENTITY}:{TOKEN}".encode()).decode()
# git never resolves the name: with a proxy it asks the proxy to connect.
REMOTE = "https://git.invalid/acme/widgets.git"


class _Proxy:
    """A forward proxy on a free loopback port that records the head of each CONNECT and
    answers it with `status`. Given `tls` it answers 200 instead, terminates TLS as the
    remote, records the head of the request git sends inside the tunnel and answers
    that one 404. Given `listener` the proxy itself speaks TLS: an https:// proxy."""

    def __init__(
        self,
        status: int = 403,
        tls: ssl.SSLContext | None = None,
        listener: ssl.SSLContext | None = None,
    ) -> None:
        self.status, self._tls, self._listener = status, tls, listener
        self.heads: list[list[str]] = []
        self.inside: list[list[str]] = []
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _Proxy:
        self._server = await asyncio.start_server(self._answer, "127.0.0.1", 0, ssl=self._listener)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        assert self._server is not None
        scheme = "https" if self._listener else "http"
        return f"{scheme}://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head.decode("latin-1").split("\r\n"))
            if self._tls is None:
                challenge = (
                    'Proxy-Authenticate: Basic realm="proxy"\r\n' if self.status == 407 else ""
                )
                answer = f"HTTP/1.1 {self.status} Refused\r\n{challenge}Content-Length: 0\r\n\r\n"
                writer.write(answer.encode())
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.start_tls(self._tls)
            inside = await reader.readuntil(b"\r\n\r\n")
            self.inside.append(inside.decode("latin-1").split("\r\n"))
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass  # git hung up: it does not trust the certificate it was shown
        finally:
            writer.close()


def _credentials(head: list[str], header: str) -> list[str]:
    """The decoded Basic pair of each `header` line in a request head."""
    values = [line.split(":", 1)[1].split() for line in head if line.lower().startswith(header)]
    return [base64.b64decode(pair).decode() for _scheme, pair in values]


def _set_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text(TOKEN + "\n")
    monkeypatch.setattr(settings, "code_git_proxy_url", url)
    monkeypatch.setattr(settings, "code_git_proxy_token_file", str(token_file))


@pytest.mark.parametrize("status", [403, 407])
async def test_git_authenticates_to_the_proxy_on_its_first_request(
    status: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "init", "--bare", "--quiet", str(mirror))
    started: list[tuple[str, ...]] = []
    spawn = asyncio.create_subprocess_exec

    async def _recorded(*argv: str, **kw: Any) -> asyncio.subprocess.Process:
        started.append(argv)
        return await spawn(*argv, **kw)

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _recorded)
    async with _Proxy(status) as proxy:
        _set_proxy(monkeypatch, tmp_path, proxy.url)
        with pytest.raises(gitrepo.GitError) as failed:
            await gitrepo.fetch(
                str(mirror), REMOTE, "main", token="repo-token", proxy_identity=IDENTITY
            )
    assert proxy.heads, "git made no request to the proxy"
    for head in proxy.heads:
        assert head[0] == "CONNECT git.invalid:443 HTTP/1.1"
        assert _credentials(head, "proxy-authorization:") == [f"{IDENTITY}:{TOKEN}"]
        # the repository's token is for the remote, inside the tunnel
        assert _credentials(head, "authorization:") == []
    (argv,) = started
    assert not [arg for arg in argv if TOKEN in arg or ENCODED in arg or BASIC in arg]
    error = str(failed.value)
    assert TOKEN not in error and ENCODED not in error and BASIC not in error
    assert tier_health.classify(error) == "egress"
    assert "proxy" not in (mirror / "config").read_text().lower()


@pytest.mark.parametrize("status", [403, 407])
async def test_a_refusal_at_an_entry_s_first_contact_names_the_proxy_and_its_status(
    status: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The clone and the `ls-remote` before an add show a fixed reason in place of
    git's words: whatever git's HTTP library calls the refusal, they name the proxy."""
    entry = repo_config(
        git_dir=str(tmp_path / "clone.git"), git_remote=REMOTE, git_proxy_identity=IDENTITY
    )
    async with _Proxy(status) as proxy:
        _set_proxy(monkeypatch, tmp_path, proxy.url)
        reason = await bootstrap.clone_mirror(entry)
        with pytest.raises(ValueError) as probe:
            await reg.remote_has_branch(
                "acme/widgets", "main", "", git_remote=REMOTE, proxy_identity=IDENTITY
            )
    assert reason == f"git clone failed: {GIT_PROXY_REFUSED} ({status})"
    assert (
        str(probe.value) == f"git ls-remote failed for acme/widgets: {GIT_PROXY_REFUSED} ({status})"
    )


def _certificate(
    name: str,
    key: ec.EllipticCurvePrivateKey,
    signed_by: tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None = None,
) -> x509.Certificate:
    """An authority's own certificate, or the certificate of the host `name` signed by
    one."""
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer, issuer_key = (signed_by[0].subject, signed_by[1]) if signed_by else (subject, key)
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=signed_by is None, path_length=None), critical=True)
    )
    if signed_by:
        try:
            host: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(name))
        except ValueError:
            host = x509.DNSName(name)
        builder = builder.add_extension(
            x509.SubjectAlternativeName([host]), critical=False
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _terminating_as(host: str, directory: Path) -> tuple[ssl.SSLContext, Path, Path]:
    """(the TLS a proxy answers with as `host`, the CA bundle that signed its certificate,
    the bundle of an authority that did not). The bundles are `authority.pem` and
    `other.pem` in `directory`."""
    pem = serialization.Encoding.PEM
    authority_key, other_key, host_key = (ec.generate_private_key(ec.SECP256R1()) for _ in "123")
    authority = _certificate("the proxy's authority", authority_key)
    other = _certificate("another authority", other_key)
    leaf = _certificate(host, host_key, signed_by=(authority, authority_key))
    files = {
        "host.crt": leaf.public_bytes(pem),
        "host.key": host_key.private_bytes(
            pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        "authority.pem": authority.public_bytes(pem),
        "other.pem": other.public_bytes(pem),
    }
    for name, data in files.items():
        (directory / name).write_bytes(data)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(directory / "host.crt"), str(directory / "host.key"))
    return tls, directory / "authority.pem", directory / "other.pem"


@pytest.mark.parametrize("trusted", [True, False], ids=["its authority", "another authority"])
async def test_git_trusts_the_remote_through_the_proxy_by_the_ca_bundle_alone(
    trusted: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A proxy that terminates TLS shows git a certificate under its own authority. git
    takes it when LOOM_CODE_GIT_PROXY_CA_FILE holds that authority and for no other
    bundle, and only then sends the repository's token, to the remote and not the proxy."""
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "init", "--bare", "--quiet", str(mirror))
    tls, authority, other = _terminating_as("git.invalid", tmp_path)
    async with _Proxy(tls=tls) as proxy:
        _set_proxy(monkeypatch, tmp_path, proxy.url)
        monkeypatch.setattr(
            settings, "code_git_proxy_ca_file", str(authority if trusted else other)
        )
        with pytest.raises(gitrepo.GitError):
            await gitrepo.remote_branches(
                str(mirror), REMOTE, token="repo-token", proxy_identity=IDENTITY
            )
    assert proxy.heads and proxy.heads[0][0] == "CONNECT git.invalid:443 HTTP/1.1"
    if not trusted:
        assert proxy.inside == []  # git ended the handshake: nothing was sent to the remote
        return
    request = proxy.inside[0]
    assert request[0] == "GET /acme/widgets.git/info/refs?service=git-upload-pack HTTP/1.1"
    assert _credentials(request, "authorization:") == ["oauth2:repo-token"]
    assert _credentials(request, "proxy-authorization:") == []


@pytest.mark.parametrize(
    ("setting", "bundle", "verified"),
    [
        ("code_git_proxy_tls_ca_file", "authority.pem", True),
        ("code_git_proxy_tls_ca_file", "other.pem", False),
        ("code_git_proxy_ca_file", "authority.pem", False),
    ],
    ids=["its authority", "another authority", "its authority as the remote's bundle"],
)
async def test_git_verifies_an_https_proxy_by_a_bundle_of_its_own(
    setting: str, bundle: str, verified: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A proxy whose own listener speaks TLS under a private authority. With that
    authority in LOOM_CODE_GIT_PROXY_TLS_CA_FILE git sends its first request, the identity
    and the password in it, as it does to an http:// proxy. With another authority's
    bundle, or with the authority named only as the remote's bundle, git refuses the
    proxy: it sends no request, so no credential leaves."""
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "init", "--bare", "--quiet", str(mirror))
    started: list[tuple[str, ...]] = []
    spawn = asyncio.create_subprocess_exec

    async def _recorded(*argv: str, **kw: Any) -> asyncio.subprocess.Process:
        started.append(argv)
        return await spawn(*argv, **kw)

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _recorded)
    listener, _authority, _other = _terminating_as("127.0.0.1", tmp_path)
    async with _Proxy(403, listener=listener) as proxy:
        assert proxy.url.startswith("https://")
        _set_proxy(monkeypatch, tmp_path, proxy.url)
        monkeypatch.setattr(settings, setting, str(tmp_path / bundle))
        with pytest.raises(gitrepo.GitError) as failed:
            await gitrepo.fetch(
                str(mirror), REMOTE, "main", token="repo-token", proxy_identity=IDENTITY
            )
    (argv,) = started
    assert not [arg for arg in argv if TOKEN in arg or ENCODED in arg or BASIC in arg]
    error = str(failed.value)
    assert TOKEN not in error and ENCODED not in error and BASIC not in error
    assert "proxy" not in (mirror / "config").read_text().lower()
    if not verified:
        assert proxy.heads == []  # git refused the proxy: no request, so no credential
        return
    assert proxy.heads, "git made no request to the proxy"
    for head in proxy.heads:
        assert head[0] == "CONNECT git.invalid:443 HTTP/1.1"
        assert _credentials(head, "proxy-authorization:") == [f"{IDENTITY}:{TOKEN}"]
        assert _credentials(head, "authorization:") == []
    assert tier_health.classify(error) == "egress"


def _checkout_with_a_submodule(directory: Path, url: str) -> Path:
    """A checkout whose one commit pins a submodule at `url`, not cloned yet."""
    directory.mkdir()
    git(directory, "init", "--quiet")
    (directory / ".gitmodules").write_text(f'[submodule "sub"]\n\tpath = sub\n\turl = {url}\n')
    git(directory, "add", ".gitmodules")
    git(directory, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    git(directory, "commit", "--quiet", "-m", "one")
    return directory


@pytest.mark.parametrize(
    ("url", "carried"),
    [("https://other.invalid/acme/sub.git", True), ("git@other.invalid:acme/sub.git", False)],
    ids=["an https submodule", "an ssh submodule"],
)
async def test_a_submodule_is_fetched_through_the_proxy_or_not_at_all(
    url: str, carried: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`git submodule update` starts a clone of its own for each submodule, on whatever
    host .gitmodules names: that clone takes the job's proxy settings, and an ssh URL,
    which no HTTP proxy carries, is a transport git refuses, never a direct connection."""
    checkout = _checkout_with_a_submodule(tmp_path / "checkout", url)
    async with _Proxy(403) as proxy:
        _set_proxy(monkeypatch, tmp_path, proxy.url)
        with pytest.raises(ToolOutputError) as failed:
            await compiler_tier.git(
                checkout,
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--quiet",
                network=True,
                token="repo-token",
                remote=REMOTE,
                proxy_identity=IDENTITY,
                timeout_s=60,
            )
    if not carried:
        assert proxy.heads == [] and "transport 'ssh' not allowed" in str(failed.value)
        return
    assert proxy.heads, "the submodule's clone made no request to the proxy"
    for head in proxy.heads:
        assert head[0] == "CONNECT other.invalid:443 HTTP/1.1"
        assert _credentials(head, "proxy-authorization:") == [f"{IDENTITY}:{TOKEN}"]
    assert tier_health.classify(str(failed.value)) == "egress"


async def test_a_clone_made_with_the_proxy_settings_stores_none_of_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    (source / "a.txt").write_text("a\n")
    git(source, "add", "a.txt")
    git(source, "commit", "--quiet", "-m", "one")
    _set_proxy(monkeypatch, tmp_path, "http://proxy.example:3128")
    # A local source needs no proxy: the settings ride along unused, as on a network clone.
    env, _secrets = await git_proxy.job_env(
        gitrepo.auth_env("", str(source)), NetworkGit(identity=IDENTITY)
    )
    clone = tmp_path / "clone.git"
    argv = ["git", "clone", "--bare", "--quiet", *gitrepo.upload_pack_args(str(source))]
    done = await asyncio.to_thread(
        subprocess.run, [*argv, str(source), str(clone)], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    kept = b"".join(p.read_bytes() for p in clone.rglob("*") if p.is_file())
    assert (clone / "config").is_file()
    for secret in (TOKEN, ENCODED, "proxy.example"):
        assert secret.encode() not in kept
