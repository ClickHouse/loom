"""What a server reachable from a browser or a network refuses: startup on a
non-loopback bind without its safeguards, CORS `*` with credentials, a foreign
Host while auth is off, URLs built from spoofed forwarded headers, and the
dashboard page's inline script without its nonce. It also checks the headers every
answer carries."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from server.boot import InsecureBindRefused, require_safe_bind
from server.settings import settings
from tests.unit.security_headers import assert_security_headers

# ---- startup refusal on a non-loopback bind ----


def _bind(**over: object) -> None:
    kw: dict[str, object] = {
        "bind_host": "0.0.0.0",
        "public_base_url": "https://loom.example",
        "auth_required": True,
        "dev_secret": False,
        "allow_insecure": False,
        "mcp_open": False,
        "weak_keys": [],
    }
    kw.update(over)
    require_safe_bind(**kw)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_bind_starts_with_the_dev_secret_or_auth_off(host: str) -> None:
    _bind(bind_host=host, dev_secret=True, auth_required=False, public_base_url="")


def test_the_dev_secret_on_a_reachable_bind_is_refused() -> None:
    with pytest.raises(InsecureBindRefused, match="LOOM_JWT_SECRET"):
        _bind(dev_secret=True)


def test_auth_off_on_a_reachable_bind_is_refused() -> None:
    with pytest.raises(InsecureBindRefused, match="auth is off"):
        _bind(bind_host="10.0.0.5", auth_required=False)


def test_mcp_open_on_a_reachable_bind_is_refused() -> None:
    """LOOM_MCP_AUTH_MODE=open runs every MCP tool with no principal, whatever auth
    the HTTP API requires."""
    with pytest.raises(InsecureBindRefused, match="LOOM_MCP_AUTH_MODE=open"):
        _bind(mcp_open=True)
    _bind(mcp_open=True, allow_insecure=True)
    _bind(bind_host="127.0.0.1", mcp_open=True)


def test_a_weak_root_key_on_a_reachable_bind_is_refused() -> None:
    with pytest.raises(InsecureBindRefused, match="below the minimum key size"):
        _bind(weak_keys=["LOOM_JWT_SECRET"])
    _bind(weak_keys=["LOOM_JWT_SECRET"], allow_insecure=True)
    with pytest.raises(InsecureBindRefused, match="gateway:gw-old is below"):
        _bind(weak_keys=["gateway:gw-old"])


def _rsa_pem(bits: int) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.mark.parametrize(
    ("secret", "weak"),
    [
        ("a" * 31, True),
        ("a" * 32, False),
        ("é" * 16, False),  # 16 characters, 32 bytes
        ("é" * 15 + "a", True),  # 31 bytes
        (1024, True),  # an RSA key of this many bits; its modulus is public in the JWKS
        (2048, False),
        ("ssh-rsa-x", True),  # PyJWT refuses it as an HMAC key; it still seals stored keys
    ],
    ids=[
        "hmac-31",
        "hmac-32",
        "multibyte-32",
        "multibyte-31",
        "rsa-1024",
        "rsa-2048",
        "not-hmac-short",
    ],
)
def test_a_weak_root_key_is_an_hmac_secret_under_32_bytes_or_rsa_under_2048_bits(
    monkeypatch: pytest.MonkeyPatch, secret: str | int, weak: bool
) -> None:
    from server.auth import keyring

    monkeypatch.setattr(
        settings, "jwt_secret", _rsa_pem(secret) if isinstance(secret, int) else secret
    )
    assert keyring.root_is_weak() is weak


def test_a_gateway_key_under_2048_bits_is_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway's RSA key verifies tokens as the root does and its modulus is public."""
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from server.auth import keyring

    def public(bits: int) -> str:
        key = rsa.generate_private_key(public_exponent=65537, key_size=bits).public_key()
        return key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def openssh(bits: int) -> str:
        key = rsa.generate_private_key(public_exponent=65537, key_size=bits).public_key()
        return key.public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        ).decode()

    keys = {
        "gw-old": public(1024),
        "gw-ssh": openssh(1024),  # PyJWT verifies with an OpenSSH key too
        "gw": public(2048),
        "broken": "not a key",
        "odd": "ssh-rsa-sha2-256 AAAAB3NzaC1yc2E=",  # a type cryptography does not know
    }
    monkeypatch.setattr(settings, "jwt_secret", "s" * 48)
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps(keys))
    assert keyring.weak_keys() == ["gateway:gw-old", "gateway:gw-ssh"]


def test_a_key_that_is_not_rsa_is_not_judged_by_its_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """PyJWT 2.10's RSA loader returns an EC or Ed25519 key unchecked; neither is weak RSA."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from jwt.algorithms import RSAAlgorithm

    from server.auth import keyring

    def pem(key: Any) -> str:
        return (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )

    monkeypatch.setattr(
        RSAAlgorithm,
        "prepare_key",
        lambda self, key: serialization.load_pem_public_key(key.encode()),
    )
    assert keyring.weak_rsa(pem(ec.generate_private_key(ec.SECP256R1()))) is False
    assert keyring.weak_rsa(pem(ed25519.Ed25519PrivateKey.generate())) is False


def test_an_end_user_issuers_inline_key_under_2048_bits_is_weak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from server.auth import keyring

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    issuers = {"https://app.example": {"orgs": ["acme"], "keys": {"k1": pem}}}
    monkeypatch.setattr(settings, "jwt_secret", "s" * 48)
    monkeypatch.setattr(settings, "jwt_end_user_issuers_json", json.dumps(issuers))
    assert keyring.weak_keys() == ["end_user:https://app.example:k1"]


def test_health_reports_mcp_open_and_a_weak_root_key_on_a_reachable_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`loom doctor` and /v1/health name what the next start would refuse."""
    from server.observability import problems

    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    monkeypatch.setattr(settings, "allow_insecure", True)
    monkeypatch.setattr(settings, "mcp_auth_mode", "open")
    monkeypatch.setattr(settings, "jwt_secret", "a" * 16)
    found = {p.code: p.severity for p in problems._exposure_problems(settings)}
    assert found["insecure_bind_mcp_open"] == "warn"
    assert found["insecure_bind_weak_key"] == "warn"
    weak = next(
        p for p in problems._exposure_problems(settings) if p.code == "insecure_bind_weak_key"
    )
    assert weak.message.startswith("LOOM_JWT_SECRET is below")
    # a reachable bind with a strong secret and MCP required reports neither
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "jwt_secret", "s" * 48)
    found = {p.code for p in problems._exposure_problems(settings)}
    assert not found & {"insecure_bind_mcp_open", "insecure_bind_weak_key"}
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    found = {p.code for p in problems._exposure_problems(settings)}
    assert not found & {"insecure_bind_mcp_open", "insecure_bind_weak_key"}


def test_a_pem_root_that_is_not_rsa_is_not_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EC key cannot sign as kid `default` at all, so the size check passes it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from server.auth import keyring

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    monkeypatch.setattr(settings, "jwt_secret", pem)
    assert keyring.root_is_weak() is False


@pytest.mark.parametrize(
    ("over", "refusal"),
    [
        ({"mcp_auth_mode": "open"}, "LOOM_MCP_AUTH_MODE=open"),
        ({"jwt_secret": "a" * 31}, "shorter than 32 bytes"),
    ],
)
def test_startup_on_a_reachable_bind_refuses_mcp_open_and_a_weak_root_key(
    monkeypatch: pytest.MonkeyPatch, over: dict[str, str], refusal: str
) -> None:
    """The server's own startup passes both checks their inputs: the refusal comes
    before it connects to anything."""
    from server import main

    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "test")
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    monkeypatch.setattr(settings, "allow_insecure", False)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "jwt_secret", "s" * 48)
    for name, value in over.items():
        monkeypatch.setattr(settings, name, value)

    def past_the_check(*_a: object, **_k: object) -> None:
        raise AssertionError("startup went past the bind check")

    monkeypatch.setattr("server.core.object_store.require_client", past_the_check)
    monkeypatch.setattr(main.boot, "require_clickhouse_ready", past_the_check)

    async def start() -> None:
        async with main.lifespan(main.app):
            pass

    with pytest.raises(InsecureBindRefused, match=refusal):
        asyncio.run(start())


def test_the_dev_stack_publishes_clickhouse_on_loopback_only() -> None:
    """The dev ClickHouse password is the checked-in default: its ports are never
    published beyond loopback, by the compose file or the per-worktree start script."""
    root = Path(__file__).resolve().parents[2]
    compose = (root / "dev/docker-compose.dev.yml").read_text()
    ports = re.findall(r'^\s*- "([^"]*):(\d+)"$', compose, flags=re.M)
    published = [p for p in ports if p[1] in ("8123", "9000")]
    assert published and all(p[0].startswith("127.0.0.1:") for p in published), published
    script = (root / "dev/ci_start_clickhouse.sh").read_text()
    flags = re.findall(r'-p "([^"]*)"', script)
    assert flags and all(f.startswith("127.0.0.1:") for f in flags), flags


def test_allow_insecure_lets_either_start() -> None:
    _bind(dev_secret=True, allow_insecure=True)
    _bind(auth_required=False, allow_insecure=True)


def test_a_reachable_bind_needs_a_public_base_url_even_with_the_override() -> None:
    for allow in (False, True):
        with pytest.raises(InsecureBindRefused, match="LOOM_PUBLIC_BASE_URL"):
            _bind(public_base_url=" ", allow_insecure=allow)


def test_a_real_secret_with_auth_on_starts() -> None:
    _bind()


# ---- CORS ----


def test_cors_star_is_refused() -> None:
    from server.auth.public_url import cors_origin_list

    with pytest.raises(RuntimeError, match="LOOM_CORS_ORIGINS"):
        cors_origin_list("https://a.example, *")
    assert cors_origin_list(" https://a.example ,https://b.example") == [
        "https://a.example",
        "https://b.example",
    ]


# ---- Host allowlist while auth is off ----


@pytest.fixture
def auth_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "cors_origins", "")


def _gated_client(base_url: str) -> TestClient:
    """An app behind the web gate and the header middleware, as server.main attaches them."""
    from fastapi import FastAPI

    from server.main import attach_browser_middleware

    app = FastAPI()
    app.get("/")(lambda: {"ok": True})
    app.post("/v1/memory.list")(lambda: {"ok": True})
    attach_browser_middleware(app)
    return TestClient(app, base_url=base_url)


def _get(base_url: str, **headers: str) -> int:
    return _gated_client(base_url).get("/", headers=headers).status_code


@pytest.mark.usefixtures("auth_off")
def test_auth_off_refuses_a_foreign_host_before_routing() -> None:
    r = _gated_client("http://rebind.evil.example").get("/")
    assert r.status_code == 421
    assert r.json()["detail"]["code"] == "MISDIRECTED_REQUEST"
    assert_security_headers(r)


@pytest.mark.usefixtures("auth_off")
@pytest.mark.parametrize(
    "base", ["http://localhost:7777", "http://127.0.0.1:7777", "http://[::1]:7777"]
)
def test_auth_off_serves_a_local_host(base: str) -> None:
    assert _get(base) == 200


@pytest.mark.usefixtures("auth_off")
def test_auth_off_serves_the_public_base_url_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    assert _get("http://loom.example") == 200
    assert _get("http://other.example") == 421


def _post(**headers: str) -> Any:
    client = _gated_client("http://localhost:7777")
    body = b'{"org":"o","namespace":"n","agent":"a"}'
    return client.post("/v1/memory.list", content=body, headers=headers)


@pytest.mark.usefixtures("auth_off")
@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
        {"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
        {"origin": "http://localhost:8080"},  # another port is another origin
        {"origin": "http://localhost:7777", "sec-fetch-site": "same-site"},
    ],
)
def test_auth_off_refuses_a_cross_site_text_plain_post(headers: dict[str, str]) -> None:
    r = _post(**{"content-type": "text/plain", **headers})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "CROSS_SITE_REQUEST"
    assert_security_headers(r)


@pytest.mark.usefixtures("auth_off")
@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, the SDKs
        {"origin": "http://localhost:7777", "sec-fetch-site": "same-origin"},
        {"origin": "http://localhost:7777", "sec-fetch-site": "none"},
    ],
)
def test_auth_off_serves_same_origin_and_originless_calls(headers: dict[str, str]) -> None:
    assert _post(**headers).status_code == 200


@pytest.mark.usefixtures("auth_off")
def test_auth_off_serves_a_configured_cors_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cors_origins", "https://app.example")
    assert _post(origin="https://app.example").status_code == 200
    assert _post(origin="https://evil.example").status_code == 403


@pytest.mark.usefixtures("auth_off")
def test_auth_off_lets_a_cross_site_link_to_a_page_through() -> None:
    nav = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
    assert _get("http://localhost:7777", **nav, **{"sec-fetch-dest": "document"}) == 200
    assert _get("http://localhost:7777", **nav, **{"sec-fetch-dest": "iframe"}) == 403
    assert _get("http://localhost:7777", **{"sec-fetch-site": "cross-site"}) == 403
    # A form submitted from another site is a navigation too, but a POST.
    form = {**nav, "sec-fetch-dest": "document", "origin": "https://evil.example"}
    assert _post(**form).status_code == 403


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "cors_origins", "")


@pytest.mark.usefixtures("auth_on")
def test_auth_on_checks_no_host_and_lets_cross_site_navigation_through() -> None:
    # A link to the dashboard or an OAuth redirect from another site is a cross-site GET.
    assert _get("http://anything.example") == 200
    assert _get("http://localhost:7777", origin="https://evil.example") == 200
    assert _get("http://localhost:7777", **{"sec-fetch-site": "cross-site"}) == 200


@pytest.mark.usefixtures("auth_on")
@pytest.mark.parametrize("path", ["/v1/memory.list", "/mcp"])
def test_auth_on_refuses_a_cross_site_post_that_carries_a_bearer(path: str) -> None:
    # Proxy mode: the front proxy adds the bearer to whatever the browser sends.
    from server.main import app

    client = TestClient(app, base_url="http://localhost:7777")
    headers = {
        "content-type": "text/plain",
        "authorization": "Bearer proxied",
        "origin": "https://evil.example",
        "sec-fetch-site": "cross-site",
    }
    r = client.post(path, content=b'{"org":"o","namespace":"n","agent":"a"}', headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "CROSS_SITE_REQUEST"
    assert_security_headers(r)


@pytest.mark.usefixtures("auth_on")
def test_auth_on_serves_same_origin_listed_and_originless_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _post().status_code == 200
    assert (
        _post(origin="http://localhost:7777", **{"sec-fetch-site": "same-origin"}).status_code
        == 200
    )
    assert _post(origin="https://evil.example").status_code == 403
    monkeypatch.setattr(settings, "cors_origins", "https://app.example")
    assert (
        _post(origin="https://app.example", **{"sec-fetch-site": "cross-site"}).status_code == 200
    )
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    assert _post(origin="https://loom.example:443").status_code == 200
    assert _post(origin="http://localhost:7777").status_code == 403


@pytest.mark.usefixtures("auth_on")
def test_auth_on_refuses_a_post_with_origin_null() -> None:
    # Origin: null is foreign even same-origin: a no-referrer page here, or a sandboxed
    # frame on another site, sends it.
    for site in ("same-origin", "cross-site"):
        r = _post(origin="null", **{"sec-fetch-site": site})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "CROSS_SITE_REQUEST"


def _post_to(base_url: str, **headers: str) -> int:
    client = _gated_client(base_url)
    return client.post("/v1/memory.list", content=b"{}", headers=headers).status_code


@pytest.mark.usefixtures("auth_on")
def test_auth_on_without_a_public_url_compares_the_origin_to_the_host() -> None:
    # Behind a TLS proxy whose X-Forwarded-Proto isn't trusted the request looks
    # like http, so only the host and port must match.
    assert _post_to("http://loom.example", origin="https://loom.example") == 200
    assert _post_to("http://loom.example:8080", origin="http://loom.example:8080") == 200
    assert _post_to("http://loom.example", origin="https://evil.example") == 403
    assert _post_to("http://loom.example", origin="https://loom.example:8443") == 403
    cross = {"origin": "https://loom.example", "sec-fetch-site": "cross-site"}
    assert _post_to("http://loom.example", **cross) == 403


@pytest.mark.usefixtures("auth_on")
def test_auth_on_with_a_public_url_compares_the_scheme_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    assert _post_to("http://loom.example", origin="https://loom.example") == 200
    assert _post_to("http://loom.example", origin="http://loom.example") == 403


# ---- response headers ----


@pytest.mark.usefixtures("auth_off")
def test_a_refusal_while_draining_closes_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import readiness

    monkeypatch.setattr(readiness, "draining", lambda: True)
    r = _post(**{"content-type": "text/plain", "origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.headers["connection"] == "close"


# Auth off, so the gate checks the preflight's Origin too.
@pytest.mark.usefixtures("auth_off")
def test_a_cors_preflight_carries_the_security_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cors_origins", "https://app.example")
    r = _gated_client("http://localhost").options(
        "/v1/memory.list",
        headers={"origin": "https://app.example", "access-control-request-method": "POST"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://app.example"
    assert_security_headers(r)


def test_a_body_limit_refusal_carries_the_security_headers() -> None:
    from server.main import app

    client = TestClient(app, base_url="http://localhost")
    r = client.post(
        "/v1/memory.list",
        content=b"{}",
        headers={"content-length": str(settings.max_request_bytes + 1)},
    )
    assert r.status_code == 413
    assert_security_headers(r)


# ---- generated URLs use the canonical base ----

SPOOF = {"x-forwarded-host": "evil.example", "x-forwarded-proto": "https"}


def _request(host: str = "localhost:7777") -> Request:
    headers = [(b"host", host.encode())] + [(k.encode(), v.encode()) for k, v in SPOOF.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("", "http://localhost:7777"), ("https://loom.example", "https://loom.example")],
)
def test_oauth_metadata_and_challenge_ignore_forwarded_host(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: str
) -> None:
    from server.auth import oauth
    from server.transport.mcp_auth import _oauth_challenge_header

    monkeypatch.setattr(settings, "oauth_issuer", "")
    monkeypatch.setattr(settings, "public_base_url", configured)
    meta = asyncio.run(oauth.as_metadata(_request())).body.decode()
    assert f'"issuer":"{expected}"' in meta and "evil" not in meta
    prm = asyncio.run(oauth.prm_metadata(_request())).body.decode()
    assert f'"resource":"{expected}/mcp"' in prm
    assert _oauth_challenge_header(_request()) == (
        f'Bearer resource_metadata="{expected}/.well-known/oauth-protected-resource"'
    )


@pytest.mark.usefixtures("auth_off")
def test_the_mcp_config_url_ignores_forwarded_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.main import app

    class _CH:
        async def query_one(self, sql: str, **kw: object) -> dict[str, int] | None:
            return {"v": 1} if "max(version)" in sql else None  # the token row's version

        async def insert(self, table: str, rows: list, **kw: object) -> None:
            pass  # the token list's row

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    client = TestClient(app, base_url="http://localhost:7777")
    body = {"client": "claude-code"}
    r = client.post("/v1/me/setup/mcp_config", json=body, headers=SPOOF)
    assert r.status_code == 200, r.text
    assert "http://localhost:7777/mcp" in r.text and "evil.example" not in r.text
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    r = client.post("/v1/me/setup/mcp_config", json=body, headers=SPOOF)
    assert "https://loom.example/mcp" in r.text and "evil.example" not in r.text


# ---- the dashboard: the boot snippet is the only inline script, and it carries the nonce ----


def _dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    from server.identity.app import dashboard_routes

    (tmp_path / "index.html").write_text(
        '<html><head><script type="module" src="/assets/i.js"></script></head></html>'
    )
    monkeypatch.setattr(dashboard_routes, "_FRONTEND_DIST", tmp_path)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "dashboard_redirect_url", "")
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = asyncio.run(dashboard_routes.dashboard_page(request))
    return response.body.decode(), response.headers["content-security-policy"]


def test_the_dashboard_csp_admits_only_the_nonced_boot_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html, csp = _dashboard(tmp_path, monkeypatch)
    script_src = next(d for d in csp.split("; ") if d.startswith("script-src"))
    nonce = re.fullmatch(r"script-src 'self' 'nonce-([A-Za-z0-9_-]{16,})'", script_src)
    assert nonce, script_src
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in csp
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", html)
    assert inline == [f' nonce="{nonce.group(1)}"']
    for directive in (
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "frame-src 'none'",
    ):
        assert directive in csp
    _, again = _dashboard(tmp_path, monkeypatch)
    assert again != csp  # a fresh nonce per response


def test_the_e2e_spec_serves_the_dashboard_csp_the_server_sends() -> None:
    """frontend/e2e/preview-isolation.spec.ts hand-copies the dashboard and download
    policies (vite preview sends no headers), so a change to either must change the spec too."""
    from server.identity.app.dashboard_routes import dashboard_csp
    from server.knowledge.app.download_routes import _DOWNLOAD_CSP

    spec = (
        Path(__file__).resolve().parents[2] / "frontend/e2e/preview-isolation.spec.ts"
    ).read_text()
    nonce = re.search(r'const NONCE = "([^"]+)";', spec)
    block = re.search(r"const DASHBOARD_CSP =(.*?);\n", spec, re.S)
    assert nonce and block
    parts = re.findall(r'"([^"]*)"|`([^`]*)`', block.group(1))
    copied = "".join(a or b for a, b in parts).replace("${NONCE}", nonce.group(1))
    assert copied == dashboard_csp(nonce.group(1))
    assert f'"Content-Security-Policy": "{_DOWNLOAD_CSP}"' in spec


# ---- `loom serve` ----


def _serve_args(
    monkeypatch: pytest.MonkeyPatch, host: str | None, reload: bool = False
) -> dict[str, object]:
    import argparse

    import uvicorn

    from server.cli.serve import _cmd_serve

    seen: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **kw: seen.update(kw))
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")
    monkeypatch.setenv("LOOM_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "test:serve")
    _cmd_serve(argparse.Namespace(host=host, port=7777, log_level="info", reload=reload))
    return seen


def test_loom_serve_binds_loopback_and_trusts_only_loopback_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve_args(monkeypatch, None)
    assert seen["host"] == "127.0.0.1"
    assert seen["forwarded_allow_ips"] == "127.0.0.1"


@pytest.mark.parametrize(("env", "loop"), [("", "asyncio"), ("uvloop", "uvloop")])
def test_loom_serve_runs_the_stdlib_loop_unless_told(
    monkeypatch: pytest.MonkeyPatch, env: str, loop: str
) -> None:
    monkeypatch.setenv("LOOM_UVICORN_LOOP", env)
    assert _serve_args(monkeypatch, None)["loop"] == loop


def test_loom_serve_host_flag_reaches_the_startup_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    seen = _serve_args(monkeypatch, "0.0.0.0")
    assert seen["host"] == "0.0.0.0"
    assert settings.bind_host == "0.0.0.0" and os.environ["LOOM_BIND_HOST"] == "0.0.0.0"


def test_loom_serve_reload_watches_only_the_server_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.paths import REPO_ROOT

    # A checkout holds Claude Code worktrees under .claude/worktrees/; watching
    # them would restart the server on every edit there.
    assert _serve_args(monkeypatch, None, reload=True)["reload_dirs"] == [str(REPO_ROOT / "server")]
    assert _serve_args(monkeypatch, None)["reload_dirs"] is None


def test_the_host_allowlist_and_the_bind_check_share_one_loopback_definition() -> None:
    from server.auth.public_url import host_header_allowed, is_loopback_host

    for host in ("localhost", "127.0.0.1", "127.0.0.2", "[::1]"):
        assert is_loopback_host(host)
        assert host_header_allowed(f"{host}:7777", "")
    assert not host_header_allowed("10.0.0.1:7777", "")
