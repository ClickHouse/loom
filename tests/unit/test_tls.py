"""TLS on the web port (`server.core.tls`): HTTPS only, the minimum version, a renewed
pair served without a restart, the last good pair kept while the files do not load, and
the refusals of a pair that cannot be served. Every handshake is real, on loopback."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import logging
import os
import socket
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from server.core import tls, web_server
from server.settings import Settings, settings


def _write(path: Path, data: bytes) -> None:
    """Replace `path` in one rename, as a certificate manager does."""
    staged = path.with_name(path.name + ".staged")
    staged.write_bytes(data)
    os.replace(staged, path)


def _pair(serial: int) -> tuple[bytes, bytes]:
    """A self-signed certificate for localhost and 127.0.0.1, and its key, as PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM), pem_key


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    cert_pem, key_pem = _pair(1)
    _write(key, key_pem)
    _write(cert, cert_pem)
    return cert, key


def _renew(files: tuple[Path, Path], serial: int) -> None:
    cert_pem, key_pem = _pair(serial)
    _write(files[1], key_pem)
    _write(files[0], cert_pem)


@contextlib.asynccontextmanager
async def _serving(context: ssl.SSLContext) -> AsyncIterator[int]:
    """A loopback TLS listener on `context` that closes each connection; its port."""

    async def close(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    server = await asyncio.start_server(close, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def _handshake(
    port: int, *, server_name: str | None = "localhost", max_version: ssl.TLSVersion | None = None
) -> tuple[int, str | None]:
    """The serial number of the certificate the port serves, and the TLS version."""
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    if max_version is not None:
        client.maximum_version = max_version
    _, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=client, server_hostname=server_name
    )
    connection = writer.get_extra_info("ssl_object")
    der = connection.getpeercert(binary_form=True)
    version = connection.version()
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return x509.load_der_x509_certificate(der).serial_number, version


def _renewing(files: tuple[Path, Path], minimum: str = "1.2") -> tls.RenewingContext:
    return tls.RenewingContext(str(files[0]), str(files[1]), tls.MIN_VERSIONS[minimum])


async def test_a_renewed_pair_is_served_without_a_restart(files: tuple[Path, Path]) -> None:
    async with _serving(_renewing(files).context) as port:
        assert (await _handshake(port))[0] == 1
        _renew(files, 2)
        assert (await _handshake(port))[0] == 2
        # A client that names no server gets the renewed pair too.
        _renew(files, 3)
        assert (await _handshake(port, server_name=None))[0] == 3


async def test_a_pair_that_does_not_load_keeps_the_last_one_until_the_files_change(
    files: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    async with _serving(_renewing(files).context) as port:
        cert_pem, key_pem = _pair(2)
        _write(files[1], key_pem)  # the new key beside the old certificate: no pair
        with caplog.at_level(logging.WARNING, logger="server.core.tls"):
            assert (await _handshake(port))[0] == 1
            assert (await _handshake(port))[0] == 1
        assert sum("do not load" in r.getMessage() for r in caplog.records) == 1
        _write(files[0], cert_pem)
        assert (await _handshake(port))[0] == 2


async def test_files_that_cannot_be_read_keep_the_last_pair(
    files: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    async with _serving(_renewing(files).context) as port:
        files[0].unlink()
        with caplog.at_level(logging.WARNING, logger="server.core.tls"):
            assert (await _handshake(port))[0] == 1
            assert (await _handshake(port))[0] == 1
        assert sum("cannot read" in r.getMessage() for r in caplog.records) == 1
        _renew(files, 2)
        assert (await _handshake(port))[0] == 2


def test_a_renewed_pair_is_served_under_uvloop(files: tuple[Path, Path]) -> None:
    uvloop = pytest.importorskip("uvloop")

    async def run() -> None:
        async with _serving(_renewing(files).context) as port:
            assert (await _handshake(port))[0] == 1
            _renew(files, 2)
            assert (await _handshake(port))[0] == 2

    uvloop.run(run())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
async def test_a_renewal_it_cannot_read_yet_is_served_once_it_can(
    files: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    async with _serving(_renewing(files).context) as port:
        _renew(files, 2)
        files[1].chmod(0)  # installed before its owner or mode is set
        with caplog.at_level(logging.WARNING, logger="server.core.tls"):
            assert (await _handshake(port))[0] == 1
            assert (await _handshake(port))[0] == 1
        assert sum("cannot read" in r.getMessage() for r in caplog.records) == 1
        files[1].chmod(0o600)  # no change the stamp sees
        assert (await _handshake(port))[0] == 2


async def test_a_version_below_the_minimum_is_refused(files: tuple[Path, Path]) -> None:
    async with _serving(_renewing(files, "1.3").context) as port:
        assert await _handshake(port) == (1, "TLSv1.3")
        # The server's alert, or a reset when the client reads after the server closed.
        with pytest.raises((ssl.SSLError, ConnectionResetError)):
            await _handshake(port, max_version=ssl.TLSVersion.TLSv1_2)
    async with _serving(_renewing(files, "1.2").context) as port:
        assert await _handshake(port, max_version=ssl.TLSVersion.TLSv1_2) == (1, "TLSv1.2")


def test_a_pair_that_does_not_load_at_start_raises(files: tuple[Path, Path]) -> None:
    _write(files[1], _pair(2)[1])
    with pytest.raises(ssl.SSLError):
        _renewing(files)


def test_an_encrypted_key_is_refused(files: tuple[Path, Path]) -> None:
    key = serialization.load_pem_private_key(files[1].read_bytes(), None)
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase"),
    )
    _write(files[1], encrypted)
    with pytest.raises(ssl.SSLError):
        _renewing(files)


@pytest.fixture
def tls_settings(monkeypatch: pytest.MonkeyPatch, files: tuple[Path, Path]) -> tuple[Path, Path]:
    monkeypatch.setattr(settings, "tls_cert_file", str(files[0]))
    monkeypatch.setattr(settings, "tls_key_file", str(files[1]))
    monkeypatch.setattr(settings, "tls_min_version", "1.3")
    monkeypatch.setattr(tls, "_served", False)
    return files


async def _scheme_app(scope: dict[str, Any], _receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": scope["scheme"].encode()})


@contextlib.asynccontextmanager
async def _uvicorn(**options: Any) -> AsyncIterator[int]:
    """uvicorn on a loopback port with `options`, as the web tier runs it; its port.
    Started and stopped as `Server.serve` does, without its signal handlers, which
    belong to pytest."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(_scheme_app, log_config=None, lifespan="off", **options)
    config.load()  # builds the TLS context, as each web process does
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    await server.startup(sockets=[sock])
    try:
        yield sock.getsockname()[1]
    finally:
        await server.shutdown(sockets=[sock])
        sock.close()


async def test_the_web_port_serves_https_only(tls_settings: tuple[Path, Path]) -> None:
    options = tls.uvicorn_options()
    assert options == {"ssl_context_factory": tls.context_factory}
    first = ssl.create_default_context(cafile=str(tls_settings[0]))
    async with _uvicorn(**options) as port:
        tls.require_served()  # the factory ran in this process
        async with httpx.AsyncClient(verify=first) as client:
            r = await client.get(f"https://localhost:{port}/")
            assert (r.status_code, r.text) == (200, "https")
        _renew(tls_settings, 2)
        renewed = ssl.create_default_context(cafile=str(tls_settings[0]))
        async with httpx.AsyncClient(verify=renewed) as client:
            assert (await client.get(f"https://localhost:{port}/")).text == "https"
        async with httpx.AsyncClient(verify=first) as client:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(f"https://localhost:{port}/")
        async with httpx.AsyncClient() as plain:
            with pytest.raises(httpx.HTTPError):
                await plain.get(f"http://127.0.0.1:{port}/")


def test_a_key_every_user_can_read_is_served_with_a_warning(
    tls_settings: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    tls_settings[1].chmod(0o600)
    with caplog.at_level(logging.WARNING, logger="server.core.tls"):
        assert tls.uvicorn_options()
        assert "every user" not in caplog.text
        tls_settings[1].chmod(0o644)
        assert tls.uvicorn_options()
    assert "every user on the host can read LOOM_TLS_KEY_FILE" in caplog.text


def test_loom_serve_serves_the_pair_and_refuses_one_that_does_not_load(
    tls_settings: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from server.cli import serve

    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "test")
    monkeypatch.setenv("LOOM_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(kw))
    args = argparse.Namespace(host="127.0.0.1", port=7790, log_level="info", reload=False)
    assert serve._cmd_serve(args) == 0
    assert started[0]["ssl_context_factory"] is tls.context_factory
    _write(tls_settings[1], _pair(2)[1])
    assert serve._cmd_serve(args) == 2
    assert len(started) == 1


def test_the_router_refuses_to_serve_plain_http_beside_the_pair(
    tls_settings: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from server.cli import router

    monkeypatch.setattr(router, "_refusal", lambda: "")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("the router started"))
    args = argparse.Namespace(host="127.0.0.1", port=7780, log_level="info")
    assert router._cmd_serve(args) == 2


def test_no_pair_serves_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tls_cert_file", "")
    monkeypatch.setattr(tls, "_served", False)
    assert tls.uvicorn_options() == {}
    tls.require_served()
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(kw))
    argv = ["--host", "0.0.0.0", "--port", "7777", "--workers", "1", "--loop", "asyncio"]
    argv += ["--timeout-graceful-shutdown", "10", "--timeout-keep-alive", "5"]
    assert web_server.main([*argv, "--forwarded-allow-ips=127.0.0.1"]) == 0
    assert "ssl_context_factory" not in started[0] and started[0]["proxy_headers"] is True


def test_a_pair_that_does_not_load_refuses_before_the_web_tier_starts(
    tls_settings: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write(tls_settings[1], _pair(2)[1])
    with caplog.at_level(logging.ERROR):
        with pytest.raises(tls.TLSRefused, match="do not load"):
            tls.uvicorn_options()
        # The reason, here a key that does not match the certificate, is logged.
        assert "KEY_VALUES_MISMATCH" in caplog.text
        caplog.clear()
        started: list[dict[str, Any]] = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(kw))
        argv = ["--host", "0.0.0.0", "--port", "7777", "--workers", "2", "--loop", "asyncio"]
        argv += ["--timeout-graceful-shutdown", "10", "--timeout-keep-alive", "5"]
        assert web_server.main([*argv, "--forwarded-allow-ips=10.0.0.0/8"]) == 2
    assert started == []
    assert "loom: LOOM_TLS_CERT_FILE and LOOM_TLS_KEY_FILE do not load" in caplog.text


def test_the_web_tier_runs_uvicorn_with_the_launchers_options(
    tls_settings: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append((app, kw)))
    argv = ["--host", "0.0.0.0", "--port", "7777", "--workers", "2", "--loop", "asyncio"]
    argv += ["--timeout-graceful-shutdown", "10", "--timeout-keep-alive", "5"]
    assert web_server.main([*argv, "--forwarded-allow-ips=10.0.0.0/8"]) == 0
    assert started == [
        (
            "server.main:app",
            {
                "host": "0.0.0.0",
                "port": 7777,
                "workers": 2,
                "loop": "asyncio",
                "timeout_graceful_shutdown": 10,
                "timeout_keep_alive": 5,
                "proxy_headers": True,
                "forwarded_allow_ips": "10.0.0.0/8",
                "ssl_context_factory": tls.context_factory,
            },
        )
    ]


class _PastTheTLSCheck(Exception):
    """Raised by the startup step after the TLS check: the check let the process start."""


def _start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the lifespan up to the object store, the step after the TLS check."""
    from server import main
    from server.core import object_store

    def past() -> None:
        raise _PastTheTLSCheck

    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "test")
    monkeypatch.setattr(object_store, "require_client", past)

    async def start() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(start())


def _context_in_child(config: uvicorn.Config) -> tuple[bool, bool]:
    """In a spawned process, as each web process of uvicorn: whether its config's factory
    built a TLS context, and whether the process now counts as serving the pair."""
    assert config.ssl_context_factory is not None
    context = config.ssl_context_factory(config, ssl.create_default_context)
    return isinstance(context, ssl.SSLContext), tls._served


def test_each_spawned_web_process_builds_its_own_context(
    tls_settings: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn pickles its config into every web process it spawns, factory included."""
    import concurrent.futures
    import multiprocessing

    monkeypatch.setenv("LOOM_ENV_FILE", "")
    monkeypatch.setenv("LOOM_TLS_CERT_FILE", str(tls_settings[0]))
    monkeypatch.setenv("LOOM_TLS_KEY_FILE", str(tls_settings[1]))
    config = uvicorn.Config("server.main:app", **tls.uvicorn_options())
    spawn = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(1, mp_context=spawn) as pool:
        assert pool.submit(_context_in_child, config).result(timeout=60) == (True, True)
    assert tls._served is False  # the child's, not this process's


def test_a_web_process_whose_pair_does_not_load_waits_then_exits(
    tls_settings: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit code 1, not uvicorn's startup failure, so uvicorn starts it again and keeps the
    other web processes; the wait bounds how often."""
    _write(tls_settings[1], _pair(2)[1])
    waits: list[float] = []
    monkeypatch.setattr("server.core.tls.time.sleep", waits.append)
    with caplog.at_level(logging.ERROR, logger="server.core.tls"):
        with pytest.raises(SystemExit) as exited:
            tls.context_factory(None, ssl.create_default_context)
    assert exited.value.code == 1 and waits == [tls.LOAD_RETRY_S]
    assert tls._served is False
    assert sum("do not load" in r.getMessage() for r in caplog.records) == 1


def test_a_process_that_serves_plain_http_with_the_pair_set_refuses_to_start(
    tls_settings: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(tls.TLSRefused, match="serves plain HTTP"):
        _start(monkeypatch)
    # The same process, once uvicorn built its context, starts.
    tls.context_factory(None, ssl.create_default_context)
    with pytest.raises(_PastTheTLSCheck):
        _start(monkeypatch)


def test_a_process_without_the_pair_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tls_cert_file", "")
    monkeypatch.setattr(tls, "_served", False)
    with pytest.raises(_PastTheTLSCheck):
        _start(monkeypatch)


@pytest.mark.parametrize("field", ["tls_cert_file", "tls_key_file"])
def test_one_file_without_the_other_fails_at_load(field: str) -> None:
    with pytest.raises(ValidationError, match="LOOM_TLS_CERT_FILE and LOOM_TLS_KEY_FILE"):
        Settings(_env_file=None, **{field: "/etc/loom/tls/x"})


def test_a_minimum_version_other_than_1_2_or_1_3_fails_at_load() -> None:
    with pytest.raises(ValidationError, match="tls_min_version"):
        Settings(_env_file=None, tls_min_version="1.1")
    # A settings file's unquoted version is a number.
    assert Settings(_env_file=None, tls_min_version=1.3).tls_min_version == "1.3"
