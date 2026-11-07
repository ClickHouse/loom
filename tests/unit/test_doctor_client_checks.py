"""`loom doctor --client` examines the machine a coding agent
runs on. Offline: HTTP is a fake, HOME and TMPDIR are temp dirs, `now` is
fixed. Every failure line names its fix."""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

import server.cli.doctor_client as dc
from server.cli import _client


def _jwt(**claims: Any) -> str:
    def b64(o: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64(claims)}.sig"


NOW = 1_800_000_000.0
TOKEN = _jwt(
    sub="bob", org="acme", namespaces=["coding", "docs"], roles=["writer"], exp=NOW + 30 * 86400
)


def _args(**kw: Any) -> argparse.Namespace:
    base = {"server_url": None, "token": None, "client": True, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _http(
    health: tuple[int, dict[str, Any]] = (
        200,
        {"status": "ok", "build_commit": "abcdef1234", "problems": []},
    ),
    mine: tuple[int, dict[str, Any]] = (
        200,
        {
            "rows": [
                {"namespace": "coding", "role": "writer"},
                {"namespace": "docs", "role": "viewer"},
            ]
        },
    ),
) -> tuple[Any, Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def get_json(url: str, token: str, timeout: float) -> tuple[int, dict[str, Any]]:
        seen["get"] = url
        return health

    def post_json(
        base_url: str, op: str, body: dict[str, Any], token: str, timeout: float
    ) -> tuple[int, dict[str, Any]]:
        # The first post is the auth probe; later ones (the recording read)
        # are kept in order.
        seen.setdefault("post", (base_url, op, body, token))
        seen.setdefault("posts", []).append(op)
        return mine

    return get_json, post_json, seen


def _grades(checks: list[tuple[str, str, str]]) -> dict[str, str]:
    return {n: g for n, g, _ in checks}


def _detail(checks: list[tuple[str, str, str]], name: str) -> str:
    return next(d for n, _, d in checks if n == name)


# ---- resolution mirrors the hooks ----


def test_resolution_order_matches_the_hooks(tmp_path: Path) -> None:
    (tmp_path / ".loom").mkdir()
    (tmp_path / ".loom" / "config.json").write_text(
        json.dumps({"url": "https://cfg/", "token": "cfg.tok", "namespace": "ns-cfg"})
    )
    (tmp_path / ".loom" / "token").write_text("file.tok\n")
    assert _client.resolve_server_url("https://flag/", tmp_path, {}) == (
        "https://flag",
        "--server-url",
    )
    assert _client.resolve_server_url(None, tmp_path, {"LOOM_BASE_URL": "https://env/"}) == (
        "https://env",
        "LOOM_BASE_URL",
    )
    assert _client.resolve_server_url(None, tmp_path, {}) == ("https://cfg", "~/.loom/config.json")
    assert _client.resolve_token(None, tmp_path, {"LOOM_API_KEY": "env.tok"}) == (
        "env.tok",
        "LOOM_API_KEY",
    )
    assert _client.resolve_token(None, tmp_path, {}) == ("cfg.tok", "~/.loom/config.json")
    (tmp_path / ".loom" / "config.json").write_text("{}")
    assert _client.resolve_token(None, tmp_path, {}) == (
        "file.tok",
        str(tmp_path / ".loom" / "token"),
    )
    assert _client.resolve_server_url(None, tmp_path, {}) == (_client.DEFAULT_BASE_URL, "default")
    assert dc.intended_namespace(tmp_path, {"LOOM_NAMESPACE": "x"}, {}) == "x"
    assert dc.intended_namespace(tmp_path, {}, {"namespaces": ["*", "coding"]}) == "coding"
    assert dc.intended_namespace(tmp_path, {}, {}) == "coding"
    (tmp_path / ".loom" / "token").unlink()
    assert _client.resolve_token(None, tmp_path, {}) == ("", "none")


def test_probe_body_names_a_scoped_tokens_first_namespace() -> None:
    """A per-namespace token gets 400 on an org-only probe, so the probe names
    its first concrete namespace."""
    assert dc.probe_body({"org": "acme", "namespaces": ["jdoe", "clickgap-public"]}) == {
        "org": "acme",
        "namespace": "jdoe",
    }
    assert dc.probe_body({"org": "acme", "namespaces": ["*"]}) == {"org": "acme"}
    assert dc.probe_body({"org": "acme", "namespaces": ["*", "docs"]}) == {
        "org": "acme",
        "namespace": "docs",
    }
    assert dc.probe_body({}) == {"org": "dev"}


# ---- the happy path ----


def test_healthy_client_passes_every_line(tmp_path: Path) -> None:
    get_json, post_json, seen = _http()
    env = {"LOOM_BASE_URL": "https://loom.test", "LOOM_API_KEY": TOKEN}
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env=env,
        tmp=tmp_path,
        now=NOW,
    )
    g = _grades(checks)
    assert g["client:server"] == "PASS" and "abcdef123" in _detail(checks, "client:server")
    assert g["client:token"] == "PASS" and "bob @ acme" in _detail(checks, "client:token")
    assert g["client:auth"] == "PASS"
    assert g["client:namespace"] == "PASS" and "'coding' as writer" in _detail(
        checks, "client:namespace"
    )
    assert g["client:reads"] == "PASS" and "2 namespace(s): coding, docs" in _detail(
        checks, "client:reads"
    )
    assert g["client:flush"] == "PASS"
    assert seen["get"] == "https://loom.test/v1/health"
    assert seen["post"] == (
        "https://loom.test",
        "namespace.list_mine",
        {"org": "acme", "namespace": "coding"},
        TOKEN,
    )
    # no ~/.claude or ~/.codex here: no client-wiring lines, no noise
    assert not [n for n in g if n.startswith("client:claude-code") or n.startswith("client:codex")]


# ---- each way the first hour goes silent ----


def test_unreachable_server_fails_and_skips_auth(tmp_path: Path) -> None:
    get_json, post_json, _ = _http(health=(0, {"detail": "ConnectionRefusedError"}))
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env={"LOOM_API_KEY": TOKEN},
        tmp=tmp_path,
        now=NOW,
    )
    g = _grades(checks)
    assert g["client:server"] == "FAIL" and "loom up" in _detail(checks, "client:server")
    assert g["client:auth"] == "WARN" and "not reachable" in _detail(checks, "client:auth")


def test_rejected_token_is_the_named_cause_of_vanishing_captures(tmp_path: Path) -> None:
    get_json, post_json, _ = _http(
        mine=(401, {"detail": {"code": "AUTH_INVALID", "message": "signature mismatch"}})
    )
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env={"LOOM_API_KEY": TOKEN},
        tmp=tmp_path,
        now=NOW,
    )
    d = _detail(checks, "client:auth")
    assert _grades(checks)["client:auth"] == "FAIL"
    assert "signature mismatch" in d and "captures vanish" in d and "mint" in d
    assert _grades(checks)["client:namespace"] == "WARN"


def test_a_rejected_token_tells_an_agent_to_drop_it_not_to_sign_up(tmp_path: Path) -> None:
    get_json, post_json, _ = _http(
        mine=(401, {"detail": {"code": "AUTH_INVALID", "message": "expired"}})
    )
    env = {"LOOM_API_KEY": TOKEN, "LOOM_CLIENT_ID": "ci", "LOOM_WORKLOAD_TOKEN": "proof"}
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env=env,
        tmp=tmp_path,
        now=NOW,
    )
    d = _detail(checks, "client:auth")
    assert _grades(checks)["client:auth"] == "FAIL"
    assert "drop --token and LOOM_API_KEY" in d and "namespace.list_mine" in d
    assert "loom init" not in d
    assert "so a rejected one makes captures vanish" in d


@pytest.mark.parametrize(
    ("claims", "grade"),
    [({"exp": NOW - 7200}, "FAIL"), ({"exp": NOW + 3600}, "WARN"), ({}, "FAIL")],
)
def test_an_agents_bad_token_gets_the_same_fix_as_a_rejected_one(
    claims: dict[str, Any], grade: str
) -> None:
    check = dc.token_check("t", "LOOM_API_KEY", claims, NOW, _client.DEFAULT_BASE_URL, agent=True)
    assert check[1] == grade
    assert "drop --token and LOOM_API_KEY" in check[2]
    assert "mint" not in check[2] and "re-copy" not in check[2]


def test_viewer_on_the_write_namespace_fails_with_the_writer_fix(tmp_path: Path) -> None:
    get_json, post_json, _ = _http(
        mine=(200, {"rows": [{"namespace": "coding", "role": "viewer"}]})
    )
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env={"LOOM_API_KEY": TOKEN},
        tmp=tmp_path,
        now=NOW,
    )
    d = _detail(checks, "client:namespace")
    assert _grades(checks)["client:namespace"] == "FAIL" and "needs writer" in d


def test_no_grant_on_the_write_namespace_names_the_ones_you_have(tmp_path: Path) -> None:
    get_json, post_json, _ = _http(mine=(200, {"rows": [{"namespace": "docs", "role": "admin"}]}))
    env = {"LOOM_API_KEY": TOKEN, "LOOM_NAMESPACE": "team-x"}
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env=env,
        tmp=tmp_path,
        now=NOW,
    )
    d = _detail(checks, "client:namespace")
    assert _grades(checks)["client:namespace"] == "FAIL" and "'team-x'" in d and "docs" in d


def test_token_lines_expired_soon_and_missing() -> None:
    assert (
        dc.token_check(
            _jwt(exp=NOW - 7200, sub="a", org="o"),
            "LOOM_API_KEY",
            {"exp": NOW - 7200},
            NOW,
            _client.DEFAULT_BASE_URL,
            agent=False,
        )[1]
        == "FAIL"
    )
    soon = dc.token_check(
        "t",
        "LOOM_API_KEY",
        {"exp": NOW + 3600, "sub": "a", "org": "o"},
        NOW,
        _client.DEFAULT_BASE_URL,
        agent=False,
    )
    assert soon[1] == "WARN" and "expires in 1 h" in soon[2]
    assert dc.token_check("", "none", {}, NOW, _client.DEFAULT_BASE_URL, agent=False)[1] == "WARN"
    assert (
        dc.token_check("not-a-jwt", "LOOM_API_KEY", {}, NOW, _client.DEFAULT_BASE_URL, agent=False)[
            1
        ]
        == "FAIL"
    )
    sa = dc.token_check(
        "t",
        "LOOM_API_KEY",
        {"sub": "sa:x", "org": "o", "sa": True, "namespaces": ["n"]},
        NOW,
        _client.DEFAULT_BASE_URL,
        agent=False,
    )
    assert sa[1] == "PASS" and sa[2].startswith("service account sa:x @ o")


def test_degraded_server_warns_with_the_error_codes() -> None:
    get_json, _, _ = _http(
        health=(
            503,
            {
                "status": "degraded",
                "build_commit": "b",
                "problems": [
                    {"code": "fake_llm", "severity": "error"},
                    {"code": "x", "severity": "warn"},
                ],
            },
        )
    )
    (name, grade, detail), _ = dc.server_check("https://h", "LOOM_BASE_URL", get_json)
    assert (
        grade == "WARN"
        and "fake_llm" in detail
        and "x" not in detail.split("reports")[1].split(".")[0].replace("fake_llm", "")
    )


# ---- client wiring ----


def test_claude_code_wiring_is_read_from_the_files_claude_code_reads(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "loom": {"type": "http", "url": "https://loom.test/mcp?toolset=coding"}
                }
            }
        )
    )
    checks = dc.claude_code_checks(tmp_path, "https://loom.test")
    g = _grades(checks)
    assert g["client:claude-code:mcp"] == "PASS"
    assert g[
        "client:claude-code:plugin"
    ] == "WARN" and "claude plugin install loom@loom" in _detail(
        checks, "client:claude-code:plugin"
    )
    # a different server is not "registered" (no plugin yet either)
    assert (
        _grades(dc.claude_code_checks(tmp_path, "https://other.test"))["client:claude-code:mcp"]
        == "WARN"
    )
    (tmp_path / ".claude" / "plugins" / "data" / "loom").mkdir(parents=True)
    assert (
        _grades(dc.claude_code_checks(tmp_path, "https://loom.test"))["client:claude-code:plugin"]
        == "PASS"
    )


def test_codex_wiring_is_read_from_config_toml_and_the_plugin_cache(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.loom]\nurl = "https://loom.test/mcp?toolset=coding"\nbearer_token_env_var = "LOOM_API_KEY"\n'
    )
    checks = dc.codex_checks(tmp_path, "https://loom.test")
    g = _grades(checks)
    assert g["client:codex:mcp"] == "PASS"
    assert g["client:codex:plugin"] == "WARN" and "codex plugin add loom@loom" in _detail(
        checks, "client:codex:plugin"
    )
    (tmp_path / ".codex" / "config.toml").write_text(
        '[plugins."loom@loom"]\nenabled = true\n'
        '[mcp_servers.loom]\nurl = "https://loom.test/mcp?toolset=coding"\n'
    )
    # another marketplace's copy is not the one the skills and the header helper read
    (tmp_path / ".codex" / "plugins" / "cache" / "zz-old" / "loom" / "0.8.0").mkdir(parents=True)
    assert _grades(dc.codex_checks(tmp_path, "https://loom.test"))["client:codex:plugin"] == "WARN"
    (tmp_path / ".codex" / "plugins" / "cache" / "loom" / "loom" / "0.9.38").mkdir(parents=True)
    assert _grades(dc.codex_checks(tmp_path, "https://loom.test"))["client:codex:plugin"] == "PASS"
    (tmp_path / ".codex" / "config.toml").write_text("")
    assert "enable `loom@loom`" in _detail(
        dc.codex_checks(tmp_path, "https://loom.test"), "client:codex:mcp"
    )


def test_codex_plugin_mcp_mount_is_checked_without_redundant_user_entry(tmp_path: Path) -> None:
    codex = tmp_path / ".codex"
    plugin = codex / "plugins" / "cache" / "loom" / "loom" / "0.9.38"
    plugin.mkdir(parents=True)
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "loom": {
                        "type": "http",
                        "url": "https://loom.test/mcp?toolset=core,coding,tracker,tasks",
                        "http_headers_helper": "node helper.mjs",
                    }
                }
            }
        )
    )
    old = plugin.parent / "0.9.9"
    old.mkdir()
    (old / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "loom": {
                        "url": "https://old.test/mcp",
                    }
                }
            }
        )
    )
    config = codex / "config.toml"
    config.write_text('[plugins."loom@loom"]\nenabled = true\n')
    checks = dc.codex_checks(tmp_path, "https://loom.test")
    assert _grades(checks)["client:codex:mcp"] == "PASS"
    assert "plugin" in _detail(checks, "client:codex:mcp")

    config.write_text('[plugins."loom@loom"]\nenabled = false\n')
    disabled = _grades(dc.codex_checks(tmp_path, "https://loom.test"))
    assert disabled["client:codex:mcp"] == "WARN"
    assert disabled["client:codex:plugin"] == "WARN"

    # Codex replaces the bundled entry whole when the user configures loom.
    config.write_text(
        '[plugins."loom@loom"]\nenabled = true\n'
        '[mcp_servers.loom]\nurl = "https://other.test/mcp"\n'
    )
    assert _grades(dc.codex_checks(tmp_path, "https://loom.test"))["client:codex:mcp"] == "WARN"


def test_claude_code_registration_token_is_probed(tmp_path: Path) -> None:
    """The bearer Claude Code itself sends — the one a
    'failed to connect' comes from — is checked, not only the hooks' token."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "loom": {
                        "type": "http",
                        "url": "https://loom.test/mcp",
                        "headers": {"Authorization": f"Bearer {TOKEN}"},
                    }
                }
            }
        )
    )
    _, rejected, _ = _http(
        mine=(401, {"detail": {"code": "AUTH_INVALID", "message": "kid unknown"}})
    )
    checks = dc.claude_code_checks(tmp_path, "https://loom.test", post_json=rejected, now=NOW)
    g = _grades(checks)
    assert g["client:claude-code:mcp-token"] == "FAIL"
    d = _detail(checks, "client:claude-code:mcp-token")
    assert "~/.claude.json mcpServers.loom" in d and "kid unknown" in d and "failed to connect" in d
    assert "claude mcp remove" in d
    _, accepted, _ = _http()
    ok = dc.claude_code_checks(tmp_path, "https://loom.test", post_json=accepted, now=NOW)
    assert _grades(ok)["client:claude-code:mcp-token"] == "PASS"
    assert "bob" in _detail(ok, "client:claude-code:mcp-token")
    # a helper-driven or OAuth entry has no static bearer: a note, not a failure
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "loom": {"type": "http", "url": "https://loom.test/mcp", "headersHelper": "x"}
                }
            }
        )
    )
    note = dc.claude_code_checks(tmp_path, "https://loom.test", post_json=accepted, now=NOW)
    assert _grades(note)["client:claude-code:mcp-token"] == "PASS" and "headersHelper" in _detail(
        note, "client:claude-code:mcp-token"
    )
    # an expired bearer fails without a network call
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "loom": {
                        "url": "https://loom.test/mcp",
                        "headers": {
                            "Authorization": "Bearer " + _jwt(sub="a", org="o", exp=NOW - 10)
                        },
                    }
                }
            }
        )
    )
    exp = dc.claude_code_checks(tmp_path, "https://loom.test", post_json=None, now=NOW)
    assert _grades(exp)["client:claude-code:mcp-token"] == "FAIL" and "expired" in _detail(
        exp, "client:claude-code:mcp-token"
    )


def test_codex_registration_env_var_is_checked_and_probed(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.loom]\nurl = "https://loom.test/mcp?toolset=coding"\nbearer_token_env_var = "LOOM_API_KEY"\n'
    )
    _, accepted, _ = _http()
    missing = dc.codex_checks(tmp_path, "https://loom.test", env={}, post_json=accepted, now=NOW)
    assert _grades(missing)["client:codex:mcp-token"] == "FAIL"
    assert "$LOOM_API_KEY" in _detail(
        missing, "client:codex:mcp-token"
    ) and "export LOOM_API_KEY" in _detail(missing, "client:codex:mcp-token")
    present = dc.codex_checks(
        tmp_path, "https://loom.test", env={"LOOM_API_KEY": TOKEN}, post_json=accepted, now=NOW
    )
    assert _grades(present)["client:codex:mcp-token"] == "PASS"
    _, rejected, _ = _http(mine=(401, {"detail": {"message": "expired"}}))
    bad = dc.codex_checks(
        tmp_path, "https://loom.test", env={"LOOM_API_KEY": TOKEN}, post_json=rejected, now=NOW
    )
    assert _grades(bad)["client:codex:mcp-token"] == "FAIL" and "re-export LOOM_API_KEY" in _detail(
        bad, "client:codex:mcp-token"
    )
    # an entry without bearer_token_env_var (OAuth) adds no token line
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.loom]\nurl = "https://loom.test/mcp"\n'
    )
    none = dc.codex_checks(tmp_path, "https://loom.test", env={}, post_json=accepted, now=NOW)
    assert "client:codex:mcp-token" not in _grades(none)


def test_stranded_flush_jobs_are_a_warning_with_the_cause(tmp_path: Path) -> None:
    now_ms = int(NOW * 1000)
    (tmp_path / f"loom-flush-{now_ms - 2_000_000}-1.json").write_text("{}")  # 33 min old
    (tmp_path / f"loom-flush-{now_ms - 1000}-2.json").write_text("{}")  # in flight
    (tmp_path / "unrelated.json").write_text("{}")
    name, grade, detail = dc.flush_check(tmp_path, NOW)
    assert grade == "WARN" and "1 capture job" in detail and "fail open" in detail
    (tmp_path / f"loom-flush-{now_ms - 2_000_000}-1.json").unlink()
    name, grade, detail = dc.flush_check(tmp_path, NOW)
    assert grade == "PASS" and "1 in flight" in detail


# ---- the doctor carries the lines ----


def test_doctor_client_flag_appends_the_lines_and_json_carries_them(
    monkeypatch: Any, capsys: Any, tmp_path: Path
) -> None:
    from server.cli import doctor

    monkeypatch.setattr(doctor, "_run_doctor_checks", lambda: [("python", "PASS", "3.12")])
    monkeypatch.setattr(doctor, "_doctor_problems", lambda: [])
    monkeypatch.setattr(
        doctor,
        "_client_checks",
        lambda args: [("client:server", "FAIL", "cannot reach x. Fix: loom up")],
    )
    rc = doctor._cmd_doctor(argparse.Namespace(json=True, client=True, server_url=None, token=None))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False
    assert out["client_checks"] == [
        {"name": "client:server", "grade": "FAIL", "detail": "cannot reach x. Fix: loom up"}
    ]
    assert [c["name"] for c in out["checks"]] == ["python", "client:server"]
    rc = doctor._cmd_doctor(
        argparse.Namespace(json=False, client=False, server_url=None, token=None)
    )
    assert rc == 0 and "client:" not in capsys.readouterr().out


def test_client_checks_never_crash_the_doctor(monkeypatch: Any) -> None:
    from server.cli import doctor

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("no network")

    monkeypatch.setattr("server.cli.doctor_client.client_checks", boom)
    lines = doctor._client_checks(argparse.Namespace(server_url=None, token=None))
    assert lines[0][1] == "FAIL" and "no network" in lines[0][2]


def test_now_defaults_to_wall_clock_and_tmp_to_tmpdir(tmp_path: Path) -> None:
    get_json, post_json, _ = _http()
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env={"TMPDIR": str(tmp_path)},
    )
    assert _grades(checks)["client:flush"] == "PASS" and str(tmp_path) in _detail(
        checks, "client:flush"
    )
    assert abs(time.time() - NOW) > 0  # the fixed NOW above is the test's, not the default


# ---- what the token reads, in the Setup page's words ----


def test_reads_line_groups_repositories_and_names_the_restricted_ones_excluded() -> None:
    rows = [
        {"namespace": "coding", "role": "writer", "kind": "memory"},
        {"namespace": "code-ch", "role": "viewer", "kind": "code"},
        {"namespace": "code-ch-26-8", "role": "viewer", "kind": "code"},
        {"namespace": "code-private", "role": "admin", "kind": "code"},
    ]
    repos = {
        "repos": [
            {
                "namespace": "code-ch",
                "repo": "org/ch",
                "branch": "master",
                "classification": "standard",
            },
            {
                "namespace": "code-ch-26-8",
                "repo": "org/ch",
                "branch": "26.8",
                "classification": "standard",
            },
            {
                "namespace": "code-private",
                "repo": "org/private",
                "branch": "master",
                "classification": "restricted",
            },
        ]
    }
    calls: list[str] = []

    def post_json(base_url: str, op: str, body: dict[str, Any], token: str, timeout: float):
        calls.append(op)
        assert op == "code.repos" and body == {"org": "acme", "consumer": "loom-doctor"}
        return 200, repos

    name, grade, detail = dc.reads_check(
        "https://loom.test", TOKEN, {"org": "acme"}, rows, post_json, True
    )
    assert (name, grade) == ("client:reads", "PASS")
    assert "1 namespace(s): coding" in detail
    assert "repositories: org/ch (2 branches)" in detail
    assert "not included: org/private" in detail and "no direct grant" in detail
    assert calls == ["code.repos"]


def test_reads_line_asks_code_repos_only_when_a_repository_row_exists() -> None:
    def post_json(*_a: Any, **_k: Any):  # pragma: no cover - must not be called
        raise AssertionError("code.repos must not be asked without a repository row")

    name, grade, detail = dc.reads_check(
        "https://loom.test",
        TOKEN,
        {"org": "acme"},
        [{"namespace": "coding", "role": "writer"}],
        post_json,
        True,
    )
    assert grade == "PASS" and detail == "reads 1 namespace(s): coding"
    skipped = dc.reads_check("https://loom.test", TOKEN, {}, [], post_json, False)
    assert skipped[1] == "WARN" and "check auth first" in skipped[2]


@pytest.mark.parametrize("namespace", ["", "team-x"])
def test_an_agent_is_checked_for_no_one_against_the_doctors_server(
    tmp_path: Path, namespace: str
) -> None:
    # A person's saved token and namespace sit beside the agent: none is used,
    # and the hint names the URL the doctor resolved (the config file's here).
    (tmp_path / ".loom").mkdir()
    (tmp_path / ".loom" / "config.json").write_text(
        json.dumps({"token": TOKEN, "namespace": "theirs", "url": "https://from-config"})
    )
    get_json, post_json, seen = _http()
    env = {"LOOM_CLIENT_ID": "ci", "LOOM_WORKLOAD_TOKEN": "proof"}
    if namespace:
        env["LOOM_NAMESPACE"] = namespace
    checks = dc.client_checks(
        _args(),
        get_json=get_json,
        post_json=post_json,
        home=tmp_path,
        env=env,
        tmp=tmp_path,
        now=NOW,
    )
    g = _grades(checks)
    assert g["client:token"] == "WARN"
    assert "loom call --server https://from-config namespace.list_mine" in _detail(
        checks, "client:token"
    )
    assert g["client:namespace"] == "WARN"
    where = "'team-x' (LOOM_NAMESPACE)" if namespace else "the exchanged token's namespace"
    assert "agent mode" in _detail(checks, "client:namespace")
    assert where in _detail(checks, "client:namespace")
    assert g["client:recording"] == g["client:reads"] == "WARN"
    assert all("check auth first" not in c[2] for c in checks if c[0].startswith("client:"))
    assert all(TOKEN not in str(call) for call in seen.values())
