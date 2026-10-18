"""Every `loom` command resolves the server URL and the token in the plugin
hooks' order: flag, environment, `~/.loom/config.json`, then the token file
or the default URL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.cli import _client


def _home(tmp_path: Path, *, config: dict[str, str] | None = None, token: str = "") -> Path:
    (tmp_path / ".loom").mkdir()
    if config is not None:
        (tmp_path / ".loom" / "config.json").write_text(json.dumps(config))
    if token:
        (tmp_path / ".loom" / "token").write_text(token + "\n")
    return tmp_path


@pytest.mark.parametrize(
    ("explicit", "env", "config", "file_token", "want"),
    [
        ("flag", {"LOOM_API_KEY": "env"}, {"token": "cfg"}, "file", ("flag", "--token")),
        (None, {"LOOM_API_KEY": "env"}, {"token": "cfg"}, "file", ("env", "LOOM_API_KEY")),
        (None, {}, {"token": "cfg"}, "file", ("cfg", _client.CONFIG_SOURCE)),
        (None, {}, {"url": "u"}, "file", ("file", "<file>")),
        (None, {"LOOM_API_KEY": "  "}, None, "", ("", "none")),
        ("  ", {}, None, "file", ("file", "<file>")),
    ],
)
def test_token_order(
    tmp_path: Path,
    explicit: str | None,
    env: dict[str, str],
    config: dict[str, str] | None,
    file_token: str,
    want: tuple[str, str],
) -> None:
    home = _home(tmp_path, config=config, token=file_token)
    token_file = str(home / ".loom" / "token")
    expected = (want[0], token_file if want[1] == "<file>" else want[1])
    assert _client.resolve_token(explicit, home, env) == expected


def test_token_file_and_config_file_overrides(tmp_path: Path) -> None:
    home = _home(tmp_path, token="home")
    (tmp_path / "alt.tok").write_text("alt\n")
    (tmp_path / "alt.json").write_text(json.dumps({"token": "alt-cfg", "url": "https://alt/"}))
    env = {"LOOM_API_KEY_FILE": str(tmp_path / "alt.tok")}
    assert _client.resolve_token(None, home, env) == ("alt", str(tmp_path / "alt.tok"))
    env["LOOM_CONFIG_FILE"] = str(tmp_path / "alt.json")
    assert _client.resolve_token(None, home, env) == ("alt-cfg", _client.CONFIG_SOURCE)
    assert _client.resolve_server_url(None, home, env) == ("https://alt", _client.CONFIG_SOURCE)


def test_server_url_order(tmp_path: Path) -> None:
    home = _home(tmp_path, config={"url": "https://cfg/"})
    env = {"LOOM_BASE_URL": "https://env/"}
    assert _client.resolve_server_url("https://flag/", home, env) == (
        "https://flag",
        "--server-url",
    )
    assert _client.resolve_server_url(None, home, env) == ("https://env", "LOOM_BASE_URL")
    assert _client.resolve_server_url(None, home, {}) == ("https://cfg", _client.CONFIG_SOURCE)
    (home / ".loom" / "config.json").write_text("[]")
    assert _client.resolve_server_url(None, home, {}) == (_client.DEFAULT_BASE_URL, "default")


def test_the_default_base_url_reads_the_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOOM_BASE_URL", raising=False)
    monkeypatch.delenv("LOOM_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HOME", str(_home(tmp_path, config={"url": "https://cfg/"})))
    assert _client.default_base_url() == "https://cfg"


_AGENT = {
    "LOOM_CLIENT_ID": "ci",
    "LOOM_WORKLOAD_TOKEN": "proof",
    "LOOM_BASE_URL": "https://loom.example",
}


def _save_agent_token(home: Path, **fields: object) -> None:
    import hashlib
    import time

    key = hashlib.sha256(b"ci").hexdigest()[:16]
    saved = {
        "client_id": "ci",
        "server": "https://loom.example",
        "proof": hashlib.sha256(b"proof").hexdigest(),
        "token": "agent.tok",
        "exp": time.time() + 600,
        **fields,
    }
    (home / ".loom" / f"agent-token-{key}.json").write_text(json.dumps(saved))


def test_an_agent_never_resolves_the_persons_saved_token(tmp_path: Path) -> None:
    home = _home(tmp_path, config={"token": "personal", "namespace": "theirs"}, token="file")
    assert _client.resolve_token(None, home, _AGENT) == ("", "agent mode: no exchanged token")
    _save_agent_token(home)
    token, source = _client.resolve_token(None, home, _AGENT)
    assert token == "agent.tok" and source.endswith(".json")
    # Another proof, another server or an expired one is not this agent's.
    for stale in ({"proof": "x"}, {"server": "https://other"}, {"exp": 1}):
        _save_agent_token(home, **stale)
        assert _client.resolve_token(None, home, _AGENT)[0] == ""
    # The explicit flag and LOOM_API_KEY still win.
    assert _client.resolve_token(None, home, {**_AGENT, "LOOM_API_KEY": "env"})[0] == "env"


def test_doctor_never_reads_the_persons_namespace_for_an_agent(tmp_path: Path) -> None:
    from server.cli.doctor_client import intended_namespace

    home = _home(tmp_path, config={"token": "personal", "namespace": "theirs"})
    assert intended_namespace(home, dict(_AGENT), {"namespaces": ["ci-review"]}) == "ci-review"
    assert intended_namespace(home, {}, {}) == "theirs"
