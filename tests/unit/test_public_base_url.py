"""The address the server hands to clients, and the Host allowlist helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.auth.public_url import host_header_allowed, is_loopback_host, public_base_url


def _req(headers: dict[str, str], scheme: str = "http") -> SimpleNamespace:
    return SimpleNamespace(headers=headers, url=SimpleNamespace(scheme=scheme))


def test_a_configured_base_url_always_wins() -> None:
    r = _req({"host": "evil.example", "x-forwarded-host": "evil.example"}, scheme="http")
    assert public_base_url(r, configured="https://loom.example/") == "https://loom.example"


def test_spoofed_forwarded_headers_are_ignored() -> None:
    # X-Forwarded-Proto reaches request.url.scheme only through uvicorn's trusted
    # proxy list; the raw headers are never read.
    r = _req(
        {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "evil.example",
            "host": "localhost:7777",
        }
    )
    assert public_base_url(r, configured="") == "http://localhost:7777"


def test_the_scheme_comes_from_the_request_url() -> None:
    r = _req({"host": "localhost:7777"}, scheme="https")
    assert public_base_url(r, configured="") == "https://localhost:7777"
    assert public_base_url(_req({"host": "loom.example"}, scheme="ws"), configured="") == (
        "http://loom.example"
    )


def test_without_a_host_the_fallback_names_localhost() -> None:
    assert public_base_url(_req({}), configured="") == "http://localhost:7777"


def test_a_malformed_host_is_not_echoed() -> None:
    for bad in ("evil.example/path", "a b", "x@evil.example", "evil.example:99999999", "<script>"):
        assert public_base_url(_req({"host": bad}), configured="") == "http://localhost:7777", bad


def test_ip_literals_and_ports_pass() -> None:
    assert public_base_url(_req({"host": "10.0.0.5:8080"}), configured="") == "http://10.0.0.5:8080"
    assert public_base_url(_req({"host": "[::1]:7777"}), configured="") == "http://[::1]:7777"


def test_the_oauth_issuer_follows_the_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import oauth
    from server.settings import settings

    r = _req({"x-forwarded-host": "evil.example", "host": "localhost:7777"})
    monkeypatch.setattr(settings, "oauth_issuer", "")
    monkeypatch.setattr(settings, "public_base_url", "")
    assert oauth.issuer(r) == "http://localhost:7777"
    monkeypatch.setattr(settings, "public_base_url", "https://public.example/")
    assert oauth.issuer(r) == "https://public.example"
    monkeypatch.setattr(settings, "oauth_issuer", "https://issuer.example/")
    assert oauth.issuer(r) == "https://issuer.example"


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.8.9.1", "::1", "[::1]", "localhost", "LOCALHOST"]
)
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "loom.example", ""])
def test_non_loopback_hosts(host: str) -> None:
    assert not is_loopback_host(host)


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:7777", "127.0.0.1:7777", "[::1]:7777", "loom.example:443"]
)
def test_local_and_public_hosts_are_allowed(host: str) -> None:
    assert host_header_allowed(host, "https://loom.example")


@pytest.mark.parametrize(
    "host", ["evil.example", "localhost.evil.example", "evil.example@localhost", "", "a b"]
)
def test_other_hosts_are_refused(host: str) -> None:
    assert not host_header_allowed(host, "https://loom.example")
    assert not host_header_allowed(host, "")
