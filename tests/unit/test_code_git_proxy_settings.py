"""The git proxy settings are checked together when they load: a URL git can use that
holds no credentials, no other proxy setting without it, and a CA bundle for the proxy's
own certificate only where the proxy speaks TLS."""

from __future__ import annotations

import pytest

from server.settings import Settings

URL = "http://proxy.example:3128"
TLS_URL = "https://proxy.example:8443"


def _load(**fields: str) -> Settings:
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url", [URL, "https://proxy.example:8443", URL + "/", "http://[::1]:3128", "http://10.0.0.7:80"]
)
def test_a_proxy_named_by_scheme_host_and_port_loads(url: str) -> None:
    loaded = _load(
        code_git_proxy_url=url,
        code_git_proxy_token_file="/var/run/secrets/git-proxy/token",
        code_git_proxy_ca_file="/etc/git-proxy/ca.crt",
        code_git_proxy_identity="acme.widgets_indexer~1@b",
    )
    assert loaded.code_git_proxy_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://user:hunter2@proxy.example:3128",
        "http://user@proxy.example:3128",
        "http://proxy.example:3128/path",
        "http://proxy.example:3128?x=1",
        "socks5://proxy.example:1080",
        "proxy.example:3128",
        "http://:3128",
        "http://proxy.example",  # git's HTTP library would assume port 1080
    ],
)
def test_a_proxy_url_git_cannot_use_or_one_holding_credentials_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="LOOM_CODE_GIT_PROXY_URL") as refused:
        _load(code_git_proxy_url=url)
    assert "hunter2" not in str(refused.value)


@pytest.mark.parametrize(
    "field",
    [
        "code_git_proxy_token_file",
        "code_git_proxy_ca_file",
        "code_git_proxy_tls_ca_file",
        "code_git_proxy_identity",
    ],
)
def test_a_proxy_setting_without_the_proxy_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match=f"LOOM_{field.upper()} needs LOOM_CODE_GIT_PROXY_URL"):
        _load(**{field: "value"})


@pytest.mark.parametrize("identity", ["acme:widgets", "two words", "a/b", "x" * 129, "café"])
def test_a_default_identity_outside_its_shape_is_refused(identity: str) -> None:
    with pytest.raises(ValueError, match="LOOM_CODE_GIT_PROXY_IDENTITY"):
        _load(code_git_proxy_url=URL, code_git_proxy_identity=identity)


def test_a_bundle_for_the_proxy_s_own_certificate_loads_with_an_https_proxy() -> None:
    assert _load().code_git_proxy_tls_ca_file == ""
    # Unset with an https proxy: git checks the proxy against its default roots.
    assert _load(code_git_proxy_url=TLS_URL).code_git_proxy_tls_ca_file == ""
    loaded = _load(
        code_git_proxy_url=TLS_URL,
        code_git_proxy_ca_file="/etc/git-proxy/ca.crt",
        code_git_proxy_tls_ca_file="/etc/git-proxy/proxy-ca.crt",
    )
    assert loaded.code_git_proxy_tls_ca_file == "/etc/git-proxy/proxy-ca.crt"
    assert loaded.code_git_proxy_ca_file == "/etc/git-proxy/ca.crt"


def test_a_bundle_for_the_proxy_s_own_certificate_is_refused_with_an_http_proxy() -> None:
    """An http:// proxy has no TLS of its own: the bundle would verify nothing."""
    with pytest.raises(
        ValueError,
        match="LOOM_CODE_GIT_PROXY_TLS_CA_FILE needs an https:// LOOM_CODE_GIT_PROXY_URL",
    ):
        _load(code_git_proxy_url=URL, code_git_proxy_tls_ca_file="/etc/git-proxy/proxy-ca.crt")
