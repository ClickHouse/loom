"""`loom doctor --client` on a machine that is only a client.

The doctor names the host in a ClickHouse timeout, skips the probes a client
never needs, does not WARN about an MCP mount the plugin provides, and flags
a stale `LOOM_API_KEY` shadowing the refreshed `~/.loom/config.json`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

import server.cli.doctor_client as dc
from server.cli import doctor

Check = tuple[str, str, str]


def _grades(checks: list[Check]) -> dict[str, str]:
    return {n: g for n, g, _ in checks}


def _detail(checks: list[Check], name: str) -> str:
    return next(d for n, _, d in checks if n == name)


class _TimingOutClient:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def __enter__(self) -> _TimingOutClient:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def get(self, *a: Any, **kw: Any) -> Any:
        raise TimeoutError("The read operation timed out")


def test_clickhouse_line_names_the_probed_host_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "ch.example.com")
    monkeypatch.setattr(settings, "ch_port", 8443)
    monkeypatch.setattr(settings, "ch_secure", None)
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr("httpx.Client", lambda *a, **kw: _TimingOutClient())
    checks = doctor._run_live_checks()
    assert _grades(checks)["clickhouse_reachable"] == "FAIL"
    detail = _detail(checks, "clickhouse_reachable")
    assert detail.startswith("https://ch.example.com:8443 → TimeoutError")


def _live() -> list[Check]:
    return [
        ("python_version", "PASS", "3.12"),
        ("clickhouse_reachable", "FAIL", "https://ch.example.com:8443 → TimeoutError: timed out"),
        ("otel_erasure_index", "PASS", "skipped (ClickHouse not reachable)"),
    ]


def test_client_run_with_a_healthy_server_downgrades_the_direct_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "_run_doctor_checks", _live)
    monkeypatch.setattr(doctor, "_doctor_problems", lambda: [])
    monkeypatch.setattr(
        doctor,
        "_client_checks",
        lambda args: [("client:server", "PASS", "https://loom.test — build abc, status ok")],
    )
    rc = doctor._cmd_doctor(argparse.Namespace(json=True, client=True, server_url=None, token=None))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
    ch = next(c for c in out["checks"] if c["name"] == "clickhouse_reachable")
    assert ch["grade"] == "WARN"
    assert ch["detail"].startswith("https://ch.example.com:8443 → TimeoutError")
    assert "not required on a client machine" in ch["detail"]


def test_downgrade_needs_both_the_client_flag_and_a_clean_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "_run_doctor_checks", _live)
    monkeypatch.setattr(doctor, "_doctor_problems", lambda: [])
    # server-side run: the probe is a real failure
    rc = doctor._cmd_doctor(
        argparse.Namespace(json=True, client=False, server_url=None, token=None)
    )
    assert rc == 1 and json.loads(capsys.readouterr().out)["ok"] is False
    # client run, but the server is degraded (WARN) or unreachable (FAIL): keep the FAIL
    for grade in ("WARN", "FAIL"):
        monkeypatch.setattr(
            doctor, "_client_checks", lambda args, g=grade: [("client:server", g, "…")]
        )
        rc = doctor._cmd_doctor(
            argparse.Namespace(json=True, client=True, server_url=None, token=None)
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        ch = next(c for c in out["checks"] if c["name"] == "clickhouse_reachable")
        assert ch["grade"] == "FAIL" and "not required" not in ch["detail"]


def test_plugin_mount_is_the_wired_state_not_a_missing_entry(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "plugins" / "data" / "loom").mkdir(parents=True)
    checks = dc.claude_code_checks(tmp_path, "https://loom.test")
    g = _grades(checks)
    assert g["client:claude-code:mcp"] == "PASS"
    assert g["client:claude-code:plugin"] == "PASS"
    detail = _detail(checks, "client:claude-code:mcp")
    assert "mounted by the loom plugin" in detail
    assert "would register the server twice" in detail
    # a user-scope entry still wins the line (and its token is probed as before)
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"loom": {"type": "http", "url": "https://loom.test/mcp"}}})
    )
    assert "user-scope MCP server 'loom'" in _detail(
        dc.claude_code_checks(tmp_path, "https://loom.test"), "client:claude-code:mcp"
    )


def test_no_plugin_and_no_entry_names_both_fixes(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    checks = dc.claude_code_checks(tmp_path, "https://loom.test")
    assert _grades(checks)["client:claude-code:mcp"] == "WARN"
    d = _detail(checks, "client:claude-code:mcp")
    assert "claude plugin install loom@loom" in d and "loom install --client claude-code" in d


def _cfg(home: Path, token: str) -> None:
    (home / ".loom").mkdir(exist_ok=True)
    (home / ".loom" / "config.json").write_text(json.dumps({"token": token}))


def test_stale_loom_token_shadowing_the_config_file_is_named(tmp_path: Path) -> None:
    _cfg(tmp_path, "fresh")
    # the run resolved LOOM_API_KEY (stale) while config.json holds the fresh one
    out = dc.token_shadow_check("stale", "LOOM_API_KEY", tmp_path, {"LOOM_API_KEY": "stale"})
    assert _grades(out)["client:token-shadow"] == "WARN"
    d = _detail(out, "client:token-shadow")
    assert d.startswith("~/.loom/config.json `token` hold(s) a different token")
    assert "grep -l LOOM_API_KEY" in d


def test_claude_code_settings_env_token_is_seen_even_when_this_shell_lacks_it(
    tmp_path: Path,
) -> None:
    _cfg(tmp_path, "fresh")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"LOOM_API_KEY": "old"}})
    )
    # the doctor's shell has no LOOM_API_KEY, so it resolved config.json and passed —
    # Claude Code's hooks and MCP run with settings.json's env and are rejected
    out = dc.token_shadow_check("fresh", "~/.loom/config.json", tmp_path, {})
    assert _grades(out)["client:token-shadow"] == "WARN"
    assert "~/.claude/settings.json env.LOOM_API_KEY" in _detail(out, "client:token-shadow")


def test_matching_copies_and_a_single_source_are_silent(tmp_path: Path) -> None:
    _cfg(tmp_path, "same")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"LOOM_API_KEY": "same"}})
    )
    assert dc.token_shadow_check("same", "LOOM_API_KEY", tmp_path, {"LOOM_API_KEY": "same"}) == []
    # only one place holds a token
    assert dc.token_shadow_check("t", "~/.loom/token", Path(tmp_path / "empty"), {}) == []
    # malformed settings.json never crashes the line
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    assert dc.token_shadow_check("same", "LOOM_API_KEY", tmp_path, {"LOOM_API_KEY": "same"}) == []
