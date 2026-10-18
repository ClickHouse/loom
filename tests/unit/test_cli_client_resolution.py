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


def test_an_agent_resolves_no_saved_token_only_an_explicit_one(tmp_path: Path) -> None:
    home = _home(tmp_path, config={"token": "personal", "namespace": "theirs"}, token="file")
    # The plugin's own exchanged copy is not borrowed either: it lives minutes
    # and is bound to the server it was exchanged at.
    (home / ".loom" / "agent-token-0123456789abcdef.json").write_text('{"token": "agent.tok"}')
    assert _client.resolve_token(None, home, _AGENT) == ("", _client.AGENT_SOURCE)
    assert _client.resolve_token("flag", home, _AGENT)[0] == "flag"
    assert _client.resolve_token(None, home, {**_AGENT, "LOOM_API_KEY": "env"})[0] == "env"


def test_doctor_never_reads_the_persons_namespace_or_tokens_for_an_agent(tmp_path: Path) -> None:
    from server.cli.doctor_client import intended_namespace, token_check, token_shadow_check

    home = _home(tmp_path, config={"token": "personal", "namespace": "theirs"})
    assert intended_namespace(home, dict(_AGENT), {"namespaces": ["ci-review"]}) == "ci-review"
    assert intended_namespace(home, {}, {}) == "theirs"
    assert token_shadow_check("", _client.AGENT_SOURCE, home, dict(_AGENT)) == []
    name, grade, detail = token_check("", _client.AGENT_SOURCE, {}, 0)
    assert grade == "WARN" and "agent mode" in detail and "mint" not in detail


@pytest.mark.parametrize("token", [None, "a-persons-token"])
def test_install_refuses_in_agent_mode_whatever_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    token: str | None,
) -> None:
    import argparse

    from server.cli import install

    for k, v in _AGENT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LOOM_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert install._cmd_install(argparse.Namespace(token=token)) == 2
    assert "not installing in agent mode" in capsys.readouterr().err
    assert not (tmp_path / ".loom" / "config.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_the_doctor_points_an_agent_at_the_server_it_checked(tmp_path: Path) -> None:
    from server.cli.doctor_client import namespace_check, token_check

    _, _, detail = token_check("", _client.AGENT_SOURCE, {}, 0, "https://loom.example")
    assert "loom call --server https://loom.example namespace.list_mine" in detail
    _, grade, why = namespace_check("coding", [], "", False, agent=True)
    assert grade == "WARN" and "exchanged token's namespace" in why
