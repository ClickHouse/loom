"""The address the server hands to clients."""

from __future__ import annotations

from types import SimpleNamespace

from server.auth.public_url import public_base_url


def _req(headers: dict[str, str], scheme: str = "http") -> SimpleNamespace:
    return SimpleNamespace(headers=headers, url=SimpleNamespace(scheme=scheme))


def test_forwarded_scheme_and_host_win_over_the_plain_http_hop() -> None:
    r = _req(
        {"x-forwarded-proto": "https", "x-forwarded-host": "loom.example", "host": "loom-blue:7781"}
    )
    assert public_base_url(r, configured="") == "https://loom.example"


def test_without_forwarded_headers_the_request_names_itself() -> None:
    assert (
        public_base_url(_req({"host": "localhost:7777"}), configured="") == "http://localhost:7777"
    )
    assert public_base_url(_req({}), configured="") == "http://localhost:7777"


def test_a_proxy_that_rewrote_the_scheme_is_what_the_setting_overrides() -> None:
    # nginx said http; the operator pinned the public address.
    r = _req({"x-forwarded-proto": "http", "host": "loom.example"})
    assert public_base_url(r, configured="https://loom.example/") == "https://loom.example"


def test_chained_proxies_take_the_client_facing_hop() -> None:
    r = _req({"x-forwarded-proto": "https, http", "x-forwarded-host": "loom.example, inner"})
    assert public_base_url(r, configured="") == "https://loom.example"


def test_a_malformed_host_is_not_echoed() -> None:
    for bad in ("evil.example/path", "a b", "x@evil.example", "evil.example:99999999", "<script>"):
        r = _req({"x-forwarded-host": bad, "host": "loom.internal:7777"})
        assert public_base_url(r, configured="") == "http://loom.internal:7777", bad
    assert public_base_url(_req({"host": "evil/x"}), configured="") == "http://localhost:7777"


def test_ip_literals_and_ports_pass() -> None:
    assert public_base_url(_req({"host": "10.0.0.5:8080"}), configured="") == "http://10.0.0.5:8080"
    assert public_base_url(_req({"host": "[::1]:7777"}), configured="") == "http://[::1]:7777"


def test_only_http_and_https_schemes_are_forwarded() -> None:
    r = _req({"x-forwarded-proto": "javascript", "host": "loom.example"})
    assert public_base_url(r, configured="") == "http://loom.example"
    assert (
        public_base_url(_req({"x-forwarded-proto": "HTTPS", "host": "loom.example"}), configured="")
        == "https://loom.example"
    )


def test_the_oauth_issuer_follows_the_public_address(monkeypatch) -> None:
    from server.auth import oauth
    from server.settings import settings

    r = _req(
        {"x-forwarded-proto": "https", "x-forwarded-host": "loom.example", "host": "loom-blue:7781"}
    )
    monkeypatch.setattr(settings, "oauth_issuer", "")
    monkeypatch.setattr(settings, "public_base_url", "")
    assert oauth.issuer(r) == "https://loom.example"
    monkeypatch.setattr(settings, "public_base_url", "https://public.example/")
    assert oauth.issuer(r) == "https://public.example"
    monkeypatch.setattr(settings, "oauth_issuer", "https://issuer.example/")
    assert oauth.issuer(r) == "https://issuer.example"
