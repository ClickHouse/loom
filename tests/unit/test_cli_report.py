"""Tests for `loom report` (server/cli/doctor.py _cmd_report).

`report` reuses `_run_doctor_checks` (so we don't need to retest each
check individually — that's tests/unit/test_cli_doctor.py's job).
We test:
  - The body carries the versions + the doctor block
  - --open lands on the bug form with the versions in its Environment field
  - No secrets surface (jwt_secret is reported as boolean, never value)
  - The argparse subcommand is registered with --open
"""

from __future__ import annotations

import argparse
import urllib.parse

import pytest


@pytest.fixture(autouse=True)
def _stub_httpx_for_ch_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's ClickHouse check tries to hit a real CH host; in tests
    we don't want network. Reuse the failing stub from test_cli_doctor."""
    from tests.unit.test_cli_doctor import _failing_httpx_client

    monkeypatch.setattr("httpx.Client", _failing_httpx_client)


def test_report_body_has_versions_and_doctor_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from server.cli.doctor import _cmd_report

    rc = _cmd_report(argparse.Namespace(open=False))
    out = capsys.readouterr().out

    assert "Loom version:" in out
    assert "Python:" in out
    assert "Platform:" in out

    # Plain text for the form's text-rendered Environment field.
    assert "loom doctor:" in out
    assert "```" not in out
    assert "python_version" in out  # at least one check name appears
    assert "jwt_secret" in out
    assert "clickhouse_reachable" in out

    # report's exit code is always 0 — it doesn't gate on doctor passing
    # (the operator is filing a bug; the bug itself is the point).
    assert rc == 0


def test_report_body_redacts_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JWT secret + CH password values must never appear in the report
    body. Only presence booleans (which loom doctor already enforces)."""
    from server.cli.doctor import _cmd_report
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "supersecret-do-not-leak")
    monkeypatch.setattr(settings, "ch_password", "another-secret-1234")

    _cmd_report(argparse.Namespace(open=False))
    out = capsys.readouterr().out

    assert "supersecret-do-not-leak" not in out
    assert "another-secret-1234" not in out


def test_open_flag_builds_correct_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--open opens the bug form with the versions in its Environment
    field (issue forms prefill a field from the query param of its id)."""
    import webbrowser

    captured: dict[str, str] = {}

    def fake_open(url: str) -> bool:
        captured["url"] = url
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)

    from server.cli.doctor import _cmd_report

    rc = _cmd_report(argparse.Namespace(open=True))
    out = capsys.readouterr().out

    assert rc == 0
    assert "url" in captured
    assert captured["url"].startswith(
        "https://github.com/ClickHouse/loom/issues/new?",
    )

    parsed = urllib.parse.urlparse(captured["url"])
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["template"] == ["30_bug-report.yaml"]
    env = qs["environment"][0]

    assert "Loom version:" in env
    assert "Python:" in env
    assert "Platform:" in env
    assert "python_version" not in env  # doctor block stays on stdout

    assert "loom doctor:" in out
    # And the helper message confirms what to do next.
    assert "browser" in out.lower()


def test_open_flag_falls_back_when_browser_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """webbrowser.open returns False when no browser is registered
    (headless server, container without DISPLAY). Operator gets a
    helpful instructive message instead of silence."""
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda _u: False)

    from server.cli.doctor import _cmd_report

    _cmd_report(argparse.Namespace(open=True))
    out = capsys.readouterr().out

    assert "Could not open a browser" in out
    assert "github.com/ClickHouse/loom/issues/new" in out
