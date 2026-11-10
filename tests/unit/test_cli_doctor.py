"""Tests for `loom doctor` (server/cli/doctor.py _cmd_doctor).

The doctor's live checks: Python version, settings load, JWT secret,
ClickHouse reachability and LLM provider resolution.
Each check runs independently — a failure on one doesn't abort the
report. Exit code 0 iff every check passed.
"""

from __future__ import annotations

import argparse
import re

import pytest


def test_a_failing_code_tier_is_a_doctor_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor

    row = {
        "org": "o",
        "namespace": "code-x",
        "tier": "clang",
        "kind": "credential",
        "reason": "no credential",
        "repo": "o/x",
        "branch": "main",
        "error": "fatal: could not read Username",
        "restricted": True,  # the host's own view names it all the same
        "since": "2026-01-01T00:00:00+00:00",
        "failed_passes": 4,
        "lag_commits": 12,
        "fix": "Set the token.",
        "severity": "warn",
    }
    monkeypatch.setattr(doctor, "_doctor_tier_failures", lambda: (row,))
    lines = doctor._problem_checks(doctor._doctor_problems())
    name, grade, detail = next(line for line in lines if line[0] == "problem:code_tier_failing")
    assert grade == doctor.WARN
    # the operator's view names the entry and its error
    assert (
        "o/x@main (o/code-x) clang: no credential, 4 failed passes since "
        '2026-01-01T00:00:00+00:00, at least 12 commits behind — "fatal: could not read Username"'
    ) in detail
    assert "Fix: Set the token." in detail


def test_doctor_reports_jwt_secret_status(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jwt_secret PASS when settings.jwt_secret is non-empty; FAIL when
    empty. The user-facing message is actionable."""
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr("httpx.Client", _failing_httpx_client)

    from server.cli.doctor import _cmd_doctor

    _cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert "[FAIL]  jwt_secret" in out
    assert "LOOM_JWT_SECRET" in out
    assert "mint-token" in out  # the actionable hint


def test_doctor_skips_ch_when_host_unset(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settings_load FAILs if ch_host is empty; the CH probe must NOT
    fire (no point hitting localhost when the operator didn't tell us
    where CH is)."""
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "")
    monkeypatch.setattr("httpx.Client", _failing_httpx_client)

    from server.cli.doctor import _cmd_doctor

    rc = _cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert "[FAIL]  settings_load" in out
    assert "LOOM_CH_HOST is empty" in out
    # CH check should report "skipped" rather than spuriously failing
    assert "skipped" in out
    assert rc == 1


def test_doctor_exit_code_0_when_all_pass(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every check passes → exit code 0."""
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "ch.example.com")
    monkeypatch.setattr(settings, "ch_port", 8443)
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr("httpx.Client", _passing_httpx_client)

    from server.cli.doctor import _cmd_doctor

    rc = _cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert "[PASS]  clickhouse_preflight" in out
    assert rc == 0
    assert re.search(r"All \d+ checks passed\.", out), out


def test_doctor_exit_code_1_when_any_fail(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any check fails → exit code 1, with a 'N check(s) failed' summary."""
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "ch.example.com")
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr("httpx.Client", _failing_httpx_client)

    from server.cli.doctor import _cmd_doctor

    rc = _cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "check(s) failed" in out


# ----------------------------------------------------------------------
# httpx stubs
# ----------------------------------------------------------------------


class _FailingHTTPXClient:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def get(self, *a, **kw):
        raise RuntimeError("synthetic httpx failure")


class _PassingHTTPXResponse:
    status_code = 200


class _PassingHTTPXClient:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def get(self, *a, **kw):
        return _PassingHTTPXResponse()


def _failing_httpx_client(*a, **kw):
    return _FailingHTTPXClient()


def _passing_httpx_client(*a, **kw):
    return _PassingHTTPXClient()


@pytest.mark.parametrize(
    ("secure", "port", "password", "want"),
    [
        (None, 8443, "pw", ("https://h:8443", ("u", "pw"))),
        (None, 8123, "", ("http://h:8123", None)),
        (True, 8123, "pw", ("https://h:8123", ("u", "pw"))),
        (False, 8443, "", ("http://h:8443", None)),
    ],
)
def test_the_doctor_probes_and_queries_one_resolved_endpoint(
    secure: bool | None, port: int, password: str, want: tuple[str, tuple[str, str] | None]
) -> None:
    from types import SimpleNamespace

    from server.cli import doctor

    s = SimpleNamespace(
        ch_secure=secure, ch_port=port, ch_host="h", ch_user="u", ch_password=password
    )
    assert doctor._doctor_ch_endpoint(s) == want


def test_doctor_reports_a_reachable_bind_without_safeguards(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr("httpx.Client", _failing_httpx_client)
    from server.cli.doctor import _cmd_doctor

    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    _cmd_doctor(argparse.Namespace())
    assert "problem:public_base_url_missing" not in capsys.readouterr().out

    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    _cmd_doctor(argparse.Namespace())
    assert "problem:public_base_url_missing" in capsys.readouterr().out
