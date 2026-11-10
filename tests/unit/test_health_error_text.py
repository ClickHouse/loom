"""`/v1/health` answers without a token, so an error it shows is the error's kind (or
a Loom error's code and message); the driver's text, which can carry a statement or
the server's address, goes to the server log. CH-free."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server import health
from server.settings import settings

_DRIVER_TEXT = "Code: 210. Connection refused (ch-internal-7:9440) SELECT 1 AS one"


class _DownCH:
    async def query_one(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError(_DRIVER_TEXT)

    async def query_all(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError(_DRIVER_TEXT)


def test_an_unanswered_ping_names_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _DownCH())
    assert asyncio.run(health._ping_clickhouse()) == (False, "RuntimeError")


def test_the_rate_limiter_and_the_workers_blocks_name_the_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ratelimit as rl
    from server.workers import child

    def _boom() -> Any:
        raise RuntimeError(_DRIVER_TEXT)

    monkeypatch.setattr(rl, "get_limiter", _boom)
    monkeypatch.setattr(child, "current_runner", _boom)
    assert health._ratelimit_block() == {"error": "RuntimeError"}
    assert health._workers_blocks()["workers"] == {"error": "RuntimeError"}


def test_an_unreadable_ledger_and_deployment_config_name_the_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as ch_mod
    from server.core import deployment_config
    from server.core.schema import status

    monkeypatch.setattr(ch_mod, "get_client", lambda: _DownCH())
    monkeypatch.setattr(status, "_cache", {"at": 0.0, "value": None})
    assert asyncio.run(status.schema_status(ttl_s=0))["error"] == "RuntimeError"

    async def _read(ch: Any) -> Any:
        raise RuntimeError(_DRIVER_TEXT)

    monkeypatch.setattr(settings, "deployment", True)
    monkeypatch.setattr(deployment_config, "read_published", _read)
    monkeypatch.setitem(deployment_config.STATE, "error", "")
    asyncio.run(deployment_config.check(_DownCH()))
    assert deployment_config.STATE["error"] == "RuntimeError"


def test_a_constraint_problem_quotes_the_setting_or_nothing() -> None:
    from server.observability.problems import _constraint_message

    assert (
        _constraint_message(
            "Code: 452. Setting max_execution_time shouldn't be greater than 30. (X)"
        )
        == "Setting max_execution_time shouldn't be greater than 30"
    )
    assert _constraint_message(_DRIVER_TEXT) == "a setting constraint"


def test_a_sandbox_layout_refusal_names_no_host_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import sandbox

    path = "/srv/loom-data/mirrors/acme.git"
    monkeypatch.setattr(
        sandbox, "layout_refusal", lambda: f"sandbox: {path} is inside Loom's data; refusing"
    )
    monkeypatch.setattr(sandbox, "sandbox_state", lambda: True)
    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    problems = health._health_provider_fields(ch_ok=True)["problems"]
    text = str(problems)
    assert "repo_sandbox_layout_refused" in text and path not in text


def test_the_sandbox_readiness_check_names_no_host_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/v1/health/ready` answers without a token: the same fixed sentence."""
    from server.code.app.index import sandbox

    monkeypatch.setattr(settings, "code_sandbox_required", True)
    monkeypatch.setattr(
        sandbox, "layout_refusal", lambda: "sandbox: /srv/loom-data/x is inside Loom's data"
    )
    check = sandbox.readiness_check()
    assert not check.ok and check.reason == sandbox.LAYOUT_REFUSED
    assert "/srv" not in check.reason


def test_the_workers_block_shows_no_pass_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-process supervisor's rows carry the registry's: `/v1/health` keeps the
    error's class and an outcome's numbers, never a pass's text."""
    from server.workers import child

    class _Runner:
        def status(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "memory_expiry_sweep",
                    "last_error": f"RuntimeError: {_DRIVER_TEXT}",
                    "last_error_kind": "RuntimeError",
                    "last_outcome": {"scanned": 3, "stopped": "other-org/ns 2026-11-01"},
                }
            ]

    monkeypatch.setattr(child, "current_runner", lambda: _Runner())
    (row,) = health._workers_blocks()["workers"]
    assert "last_error" not in row and row["last_error_kind"] == "RuntimeError"
    assert row["last_outcome"] == {"scanned": 3}


def test_a_polled_failure_is_logged_once_a_minute(caplog: pytest.LogCaptureFixture) -> None:
    """Any caller can poll `/v1/health`: the driver's text is not logged per poll."""
    with caplog.at_level("WARNING", logger="server.health"):
        for _ in range(3):
            assert health._shown_error(RuntimeError(_DRIVER_TEXT), "the ping") == "RuntimeError"
    assert sum("the ping failed" in r.getMessage() for r in caplog.records) == 1


def test_an_unreached_analysis_probe_shows_no_driver_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # A probe that could not reach ClickHouse is no finding: health shows no
    # analysis problem (the replica answers OVERLOADED), and the driver's text
    # goes to the log only.
    from server.insights.app import analysis
    from server.observability import problems

    async def down() -> Any:
        raise RuntimeError(_DRIVER_TEXT)

    monkeypatch.setattr(analysis, "_ready", None)
    monkeypatch.setattr(analysis, "_problem", "")
    monkeypatch.setattr(analysis, "_unreached_kind", "")
    monkeypatch.setattr(analysis, "_unreached_since", 0.0)
    monkeypatch.setattr(analysis.store, "sealed_secrets", down)
    asyncio.run(analysis.reprobe())
    ok, error, _ = analysis.analysis_state()
    shown = problems._analysis_problems(
        settings, problems.HealthState(analysis_ok=ok, analysis_error=error)
    )
    assert shown == [] and "ch-internal-7" not in error


@pytest.mark.parametrize("manages", [False, True])
def test_an_extra_analyst_grant_names_no_database(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, manages: bool
) -> None:
    # With access management the write is stubbed out, so the grant survives it
    # and the second probe's answer is the one shown.
    from server.insights.app import analysis
    from server.insights.domain import analysis as boundary
    from server.observability import problems

    async def run(sql: str, **_: Any) -> Any:
        if sql.startswith("SELECT count()") and "system." not in sql:
            return [], [], [(1,)] if boundary.CANARY_TABLE in sql else [(0,)]
        raise RuntimeError("Code: 497. DB::Exception: Not enough privileges")

    async def returns(value: Any, *_: Any, **__: Any) -> Any:
        return value

    foreign = ("*", "GRANT SELECT ON otherdb.payroll TO loom_analyst")
    monkeypatch.setattr(analysis, "_ready", None)
    monkeypatch.setattr(analysis, "_problem", "")
    monkeypatch.setattr(analysis, "_loaded", (bytes(32), "pw"))
    monkeypatch.setattr(analysis, "_setting_names", boundary.SettingNames("SQL_"))
    monkeypatch.setattr(analysis.store, "target_database", lambda: "`loom`")
    monkeypatch.setattr(analysis.store, "sealed_secrets", lambda: returns(None))
    monkeypatch.setattr(analysis.store, "run_statement", run)
    monkeypatch.setattr(
        analysis.store, "own_version", lambda *_, **__: returns(boundary.ENTITY_VERSION)
    )
    monkeypatch.setattr(analysis, "_signed_only", lambda *_: returns(True))
    monkeypatch.setattr(
        analysis.store,
        "own_grants",
        lambda **_: returns(frozenset(boundary.allowed_columns()) | {foreign}),
    )
    monkeypatch.setattr(analysis.store, "can_manage_access", lambda: returns(manages))
    monkeypatch.setattr(analysis, "_write_under_lease", lambda: returns(("", "")))
    asyncio.run(analysis.reprobe())
    ok, error, _ = analysis.analysis_state()
    shown = problems._analysis_problems(
        settings, problems.HealthState(analysis_ok=ok, analysis_error=error)
    )
    assert shown and "grants outside the allowlist" in shown[0].message
    assert "payroll" not in shown[0].message
    assert any("payroll" in r.message for r in caplog.records)


def test_health_shows_the_analysis_finding_with_its_own_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.app import analysis

    monkeypatch.setattr(analysis, "_ready", False)
    monkeypatch.setattr(analysis, "_problem", "writing or probing loom_analyst failed: Code 36")
    monkeypatch.setattr(analysis, "_fix", "the fix for code 36")
    problems = health._health_provider_fields(ch_ok=True)["problems"]
    (shown,) = [p for p in problems if p["code"] == "analysis_user_failing"]
    assert shown["fix"] == "the fix for code 36"
