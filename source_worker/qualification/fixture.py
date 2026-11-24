"""Synthetic inputs, HTTPS KV replies, and mTLS probes for the Compose smoke."""

from __future__ import annotations

import errno
import http.client
import json
import secrets
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from loom_source_worker.direct_backend import ApprovedProfile
from loom_source_worker.main import _parse_settings
from loom_source_worker.mcp_client import MCPSourceConfig
from loom_source_worker.protocol import ReadBinding, ReadGrant, issue_read_grant

PROXY = "source-worker-proxy"
KV = "synthetic-kv"
ISSUER = "urn:loom:qualification"
ORG = "smoke-org"
OWNER = "smoke-user"
PRIVATE_MCP = "https://127.0.0.1:9443"
SIGNALS = Path("/signals")
CALLER = Path("/caller")


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(mode)


def private_bytes(key: ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def issue_certificate(name: str, ca=None, purpose=None, *, expired: bool = False):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signing_key, issuer = ca if ca is not None else (key, None)
    now = datetime.now(UTC)
    end = now - timedelta(hours=1) if expired else now + timedelta(hours=2)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=2))
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=ca is None, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=ca is not None,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca is None,
                crl_sign=ca is None,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if purpose is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
    if purpose == ExtendedKeyUsageOID.SERVER_AUTH:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False
        )
    return key, builder.sign(signing_key, hashes.SHA256())


def prepare(root: Path) -> None:
    operator = root / "source_worker/operator"
    config, credentials, proxy = (
        operator / subdir for subdir in ("worker/config", "worker/credentials", "proxy")
    )
    caller, kv, signals = (root / name for name in ("caller", "kv", "signals"))
    for directory in (config, credentials, proxy, caller, kv, signals):
        directory.mkdir(parents=True, mode=0o755)
    caller.chmod(0o700)
    kv.chmod(0o700)

    server_ca, client_ca, foreign_ca, vault_ca = (
        issue_certificate(name)
        for name in ("smoke-server-ca", "smoke-client-ca", "foreign-ca", "smoke-kv-ca")
    )
    write(caller / "server-ca.pem", server_ca[1].public_bytes(Encoding.PEM))
    write(caller / "foreign-ca.pem", foreign_ca[1].public_bytes(Encoding.PEM))
    write(proxy / "client-ca.pem", client_ca[1].public_bytes(Encoding.PEM))
    write(config / "vault-ca.pem", vault_ca[1].public_bytes(Encoding.PEM))
    for name, ca, directory, cert_name, secret_mode in (
        (PROXY, server_ca, proxy, "server-chain", 0o440),
        (KV, vault_ca, kv, "server", 0o400),
    ):
        key, cert = issue_certificate(name, ca, ExtendedKeyUsageOID.SERVER_AUTH)
        write(directory / f"{cert_name}.pem", cert.public_bytes(Encoding.PEM))
        write(directory / "server-key.pem", private_bytes(key), secret_mode)
    for name, ca in (("valid", client_ca), ("untrusted", foreign_ca), ("expired", client_ca)):
        key, cert = issue_certificate(
            name, ca, ExtendedKeyUsageOID.CLIENT_AUTH, expired=name == "expired"
        )
        write(caller / f"{name}.pem", cert.public_bytes(Encoding.PEM))
        write(caller / f"{name}-key.pem", private_bytes(key), 0o400)

    signer = ed25519.Ed25519PrivateKey.generate()
    write(caller / "signer.pem", private_bytes(signer), 0o400)
    write(
        config / "grant-verifier.pem",
        signer.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
    )
    worker_id, active, slow, missing = (str(uuid4()) for _ in range(4))
    profile = ApprovedProfile(
        MCPSourceConfig(f"{PRIVATE_MCP}/mcp", bearer_audience=PRIVATE_MCP), (ORG,)
    )
    profile_config = asdict(profile)
    del profile_config["config"]["approved_resource_ids"]
    settings = {
        "transport": "tls_proxy_unix",
        "worker_id": worker_id,
        "issuer": ISSUER,
        "vault": {
            "origin": f"https://{KV}:9443",
            "mount": "smoke",
            "custom_ca": True,
            "timeout_seconds": 10,
        },
        "read_limits": {
            "timeout_seconds": 6,
            "concurrent": 1,
            "concurrent_per_org": 1,
            "content_bytes": 4096,
        },
        "profiles": [profile_config],
    }
    raw_settings = json.dumps(settings).encode()
    [parsed_profile] = _parse_settings(raw_settings).profiles
    write(config / "config.json", raw_settings)
    bindings = {
        "worker_id": worker_id,
        "active": active,
        "slow": slow,
        "missing": missing,
        "profile_digest": parsed_profile.digest,
    }
    write(caller / "bindings.json", json.dumps(bindings).encode())
    vault_token, upstream_token = (secrets.token_urlsafe(32) for _ in range(2))
    write(credentials / "vault-token", vault_token.encode(), 0o440)
    write(
        kv / "inputs.json",
        json.dumps(dict(bindings, vault_token=vault_token, upstream_token=upstream_token)).encode(),
        0o400,
    )


def serve() -> None:
    root = Path("/kv")
    settings = json.loads((root / "inputs.json").read_text())
    target = f"/v1/smoke/data/{settings['worker_id']}/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:
            status, body = self.reply()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply(self) -> tuple[int, bytes]:
            connection = self.path.removeprefix(target)
            kind = next(
                (kind for kind in ("active", "slow", "missing") if settings[kind] == connection),
                None,
            )
            values = self.headers.get_all("X-Vault-Token", [])
            authorized = (
                self.path.startswith(target)
                and kind is not None
                and len(values) == 1
                and secrets.compare_digest(values[0], settings["vault_token"])
                and "Authorization" not in self.headers
                and "Forwarded" not in self.headers
                and "X-Qualification-Canary" not in self.headers
            )
            with (SIGNALS / "kv-events").open("ab") as events:
                events.write(f"{kind if authorized else 'unexpected'}\n".encode())
            if not authorized:
                return 403, b"{}"
            if kind == "missing":
                return 404, b"{}"
            if kind == "slow":
                time.sleep(15)
                return 404, b"{}"
            record = {
                "binding": {
                    "worker_id": settings["worker_id"],
                    "org": ORG,
                    "owner": OWNER,
                    "connection_id": connection,
                    "generation": 1,
                    "profile_digest": settings["profile_digest"],
                },
                "state": "active",
                "auth": "bearer",
                "audience": PRIVATE_MCP,
                "token": settings["upstream_token"],
                "expires_at": int(time.time()) + 3600,
            }
            return 200, json.dumps(
                {
                    "data": {
                        "data": record,
                        "metadata": {"version": 1, "destroyed": False, "deletion_time": ""},
                    }
                }
            ).encode()

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, _request, _client_address) -> None:
            if not isinstance(
                sys.exception(), (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError)
            ):
                (SIGNALS / "kv-error").touch()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(root / "server.pem", root / "server-key.pem")
    with QuietServer(("0.0.0.0", 9443), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        (SIGNALS / "kv-ready").touch()
        server.serve_forever()


def client_context(cert: str | None = "valid", ca: str = "server-ca") -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(CALLER / f"{ca}.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if cert is not None:
        context.load_cert_chain(CALLER / f"{cert}.pem", CALLER / f"{cert}-key.pem")
    return context


def request(
    context: ssl.SSLContext,
    token: str = "",
    *,
    start: str = "POST /v1/live HTTP/1.1",
    extra: bytes = b"",
    body: bytes = b"",
    hostname: str = PROXY,
) -> tuple[int, list, bytes]:
    authorization = f"Authorization: Bearer {token}\r\n" if token else ""
    headers = f"{start}\r\nHost: {PROXY}\r\nConnection: close\r\n{authorization}".encode()
    with socket.create_connection((PROXY, 8443), timeout=12) as connection:
        with context.wrap_socket(connection, server_hostname=hostname) as channel:
            channel.sendall(headers + extra + b"\r\n" + body)
            with http.client.HTTPResponse(channel) as response:
                response.begin()
                payload = response.read(65_537)
                assert len(payload) <= 65_536, "unexpected response size"
                response_headers = response.getheaders()
    if token:
        assert token.encode() not in payload and all(
            token not in val for _, val in response_headers
        )
    return response.status, response_headers, payload


def assert_reply(response: tuple[int, list, bytes], status: int, code: str) -> None:
    actual, headers, payload = response
    assert actual == status, f"expected worker {status}, got {actual}"
    assert [value for name, value in headers if name.lower() == "cache-control"] == ["no-store"]
    assert json.loads(payload) == {"status": code}, f"unexpected response for {status}"


def wait_for(check, label: str, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(label)


def observed() -> list[str]:
    assert not (SIGNALS / "kv-error").exists(), "unexpected synthetic KV I/O error"
    path = SIGNALS / "kv-events"
    return path.read_text().splitlines() if path.exists() else []


def probe(mode: str) -> None:
    bindings = json.loads((CALLER / "bindings.json").read_text())
    signer = load_pem_private_key((CALLER / "signer.pem").read_bytes(), password=None)
    assert isinstance(signer, ed25519.Ed25519PrivateKey)
    context = client_context()

    def grant(connection="missing", operation="live", *, key=signer, issuer=ISSUER, **changes):
        binding = {
            "worker_id": bindings["worker_id"],
            "org": ORG,
            "namespace": "smoke",
            "source_id": str(uuid4()),
            "selection_generation": 1,
            "requester": OWNER,
            "connection_id": bindings[connection],
            "connection_generation": 1,
            "profile_digest": bindings["profile_digest"],
            "resource_id": "smoke://fixture/document",
        }
        binding.update(changes)
        read = ReadGrant(
            request_id=uuid4(),
            operation=operation,
            binding=ReadBinding.model_validate_json(json.dumps(binding)),
        )
        return issue_read_grant(read, issuer=issuer, key=key, lifetime_seconds=30)

    if mode == "stopped":
        before = observed()
        assert request(context, grant())[0] == 502, "proxy did not fail closed without the worker"
        assert observed() == before
        return

    wait_for((SIGNALS / "kv-ready").is_file, "synthetic KV is not listening")

    last_probe = "no response"

    def ready() -> bool:
        nonlocal last_probe
        try:
            status, _, payload = request(context)
            last_probe = f"HTTP {status}"
            return status == 403 and payload == b'{"status":"denied"}'
        except ssl.SSLCertVerificationError:
            raise
        except (OSError, http.client.HTTPException) as error:
            last_probe = type(error).__name__
            return False

    try:
        wait_for(ready, "mTLS proxy or worker socket is not ready")
    except AssertionError as error:
        raise AssertionError(f"{error}; last probe: {last_probe}") from None
    before = observed()
    assert_reply(request(context, grant()), 409, "reconnect")
    assert observed() == [*before, "missing"], "signed grant did not reach the KV fixture"
    if mode == "ready":
        return
    print(
        f"Installed worker {version('loom-source-worker')}; MCP SDK {version('mcp')}; "
        f"{ssl.OPENSSL_VERSION}"
    )

    before = observed()
    for cert in (None, "untrusted", "expired"):
        invalid_context = client_context(cert)
        try:
            status, _, _ = request(invalid_context, grant())
        except ssl.SSLCertVerificationError:
            raise
        except ssl.SSLError as error:
            assert error.reason in {
                "TLSV13_ALERT_CERTIFICATE_REQUIRED",
                "TLSV1_ALERT_UNKNOWN_CA",
                "SSLV3_ALERT_BAD_CERTIFICATE",
                "SSLV3_ALERT_CERTIFICATE_EXPIRED",
            }, "unexpected client TLS alert"
            continue
        assert status == 400, "proxy did not reject an invalid client certificate"
    for ca, hostname in (("foreign-ca", PROXY), ("server-ca", "wrong-name.invalid")):
        try:
            request(client_context(ca=ca), grant(), hostname=hostname)
        except ssl.SSLCertVerificationError:
            pass
        else:
            raise AssertionError("caller accepted untrusted server identity")
    with socket.create_connection((PROXY, 8443), timeout=5) as connection:
        connection.sendall(f"POST /v1/live HTTP/1.1\r\nHost: {PROXY}\r\n\r\n".encode())
        try:
            assert connection.recv(1) == b"", "proxy returned bytes to a cleartext caller"
        except ConnectionResetError:
            pass

    first_grant = grant()
    for start, extra, body, expected in (
        ("GET /v1/live HTTP/1.1", b"", b"", 404),
        ("POST /v1/live? HTTP/1.1", b"", b"", 404),
        ("POST /v1/%6cive HTTP/1.1", b"", b"", 404),
        ("POST /v1/live HTTP/1.1", b"Content-Length: 1\r\n", b"x", 400),
        ("POST /v1/live HTTP/1.1", b"Content-Length: 00\r\n", b"", 400),
        ("POST /v1/live HTTP/1.1", b"Transfer-Encoding: chunked\r\n", b"0\r\n\r\n", 400),
        ("POST /v1/live HTTP/1.1", b"Authorization: Bearer other\r\n", b"", 400),
        ("POST /v1/live HTTP/1.1", b"X-Oversize: " + b"x" * 32_768 + b"\r\n", b"", 400),
    ):
        status, _, _ = request(context, first_grant, start=start, extra=extra, body=body)
        assert status == expected, f"proxy framing refusal: expected {expected}, got {status}"

    expired = jwt.decode(grant(), options={"verify_signature": False})
    expired.update(iat=int(time.time()) - 20, exp=int(time.time()) - 5)
    for token in (
        "",
        "not-a-grant",
        grant(key=ed25519.Ed25519PrivateKey.generate()),
        grant(issuer="wrong-issuer"),
        grant(worker_id=str(uuid4())),
        grant(operation="check_access"),
        jwt.encode(expired, signer, algorithm="EdDSA"),
    ):
        assert_reply(request(context, token), 403, "denied")
    assert observed() == before, "a refused TLS, HTTP or grant request reached the KV fixture"
    print("PASS: peer trust, cleartext and HTTP framing refused; invalid claims fail closed")

    canary = b"X-Qualification-Canary: synthetic-header\r\nForwarded: for=192.0.2.5\r\n"
    for operation, path in (("live", "live"), ("check_access", "check-access")):
        before = observed()
        assert_reply(
            request(
                context, grant(operation=operation), start=f"POST /v1/{path} HTTP/1.1", extra=canary
            ),
            409,
            "reconnect",
        )
        assert observed() == [*before, "missing"]
    before = observed()
    assert_reply(request(context, grant("active", connection_generation=2)), 403, "denied")
    assert observed() == [*before, "active"], "stale generation must fail before re-authorization"
    before = observed()
    assert_reply(request(context, grant("active")), 403, "denied")
    after = observed()
    assert after[: len(before)] == before and len(after[len(before) :]) >= 2
    assert set(after[len(before) :]) == {"active"}
    print(
        "PASS: both signed routes reach TLS KV; live worker denies private MCP and stale bindings"
    )

    before = observed()
    with ThreadPoolExecutor(max_workers=1) as pool:
        started = time.monotonic()
        pending = pool.submit(request, context, grant("slow"))
        wait_for(
            lambda: observed() == [*before, "slow"], "slow KV request was not admitted", seconds=4
        )
        assert_reply(request(context, grant()), 429, "overloaded")
        assert observed() == [*before, "slow"], "overloaded request reached KV"
        assert_reply(pending.result(timeout=10), 504, "timeout")
        elapsed = time.monotonic() - started
        assert 5 <= elapsed < 9.5, f"worker did not apply the 6s deadline: {elapsed:.2f}s"
    assert_reply(request(context, grant()), 409, "reconnect")
    assert observed() == [*before, "slow", "missing"], "timeout did not release the worker slot"
    print(
        f"PASS: stalled KV read returned at {elapsed:.2f}s; "
        "overloaded request shed and slot recovered"
    )


def assert_isolated(address: str) -> None:
    try:
        with socket.create_connection((address, 8443), timeout=2):
            raise AssertionError("proxy port reachable from worker egress")
    except TimeoutError:
        pass
    except OSError as error:
        assert error.errno in {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "prepare":
        prepare(Path("/work"))
    elif mode == "serve":
        serve()
    elif mode == "isolated":
        assert_isolated(sys.argv[2])
    else:
        assert mode in {"probe", "ready", "stopped"}
        probe(mode)
