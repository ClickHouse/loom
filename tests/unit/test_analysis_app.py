"""insights.query's handling around the boundary: how ClickHouse errors are
answered, what the probe calls repairable, and when nothing is written."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.errors import AnalysisUnavailableError, RateLimitedError, ValidationError
from server.core.ratelimit import op_class_for
from server.insights.api.models_analysis import InsightsQueryInput
from server.insights.app import analysis as app
from server.insights.domain import analysis as boundary

SECRETS = (bytes(range(32)), "pw")


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_loaded", SECRETS)
    monkeypatch.setattr(app, "_ready", True)
    monkeypatch.setattr(app, "_problem", "")
    monkeypatch.setattr(app, "_running", {})
    monkeypatch.setattr(app.store, "target_database", lambda: "`loom`")


def _failing(message: str):
    async def run(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(message)

    return run


def _query(namespace: str = "ns") -> InsightsQueryInput:
    return InsightsQueryInput(org="acme", namespace=namespace, sql="SELECT 1")


@pytest.mark.parametrize(
    ("message", "answer"),
    [
        ("Code: 202. DB::Exception: Too many simultaneous queries", RateLimitedError),
        ("Code: 516. DB::Exception: loom_analyst: Authentication failed", AnalysisUnavailableError),
        (
            "Code: 241. DB::Exception: (total) memory limit exceeded: would use 9 GiB",
            AnalysisUnavailableError,
        ),
        ("Code: 241. DB::Exception: Memory limit (for query) exceeded", ValidationError),
        ("Code: 62. DB::Exception: Syntax error", ValidationError),
    ],
)
def test_clickhouse_errors_are_answered_by_their_cause(
    monkeypatch: pytest.MonkeyPatch, ready: None, message: str, answer: type[Exception]
) -> None:
    monkeypatch.setattr(app.store, "run_statement", _failing(message))
    with pytest.raises(answer):
        asyncio.run(app.handle_insights_query(_query()))


def test_a_refusal_never_quotes_a_key_pad(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    leak = "Code: 62. DB::Exception: in filter hex(SHA256(concat(unhex('" + "36" * 64 + "'), x)))"
    monkeypatch.setattr(app.store, "run_statement", _failing(leak))
    with pytest.raises(ValidationError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert "36" * 64 not in str(caught.value)


def test_a_namespace_with_a_comma_is_refused(ready: None) -> None:
    # The policy splits the signed namespaces on commas.
    with pytest.raises(ValidationError):
        asyncio.run(app.handle_insights_query(_query("a,b")))


def test_insights_query_has_its_own_rate_limit_class() -> None:
    assert op_class_for("insights.query") == "insights.query"
    assert op_class_for("insights.catalog") == "default"


def test_a_reserved_scope_read_refused_for_want_of_grants_is_repairable(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    async def canary(_: Any) -> bool:
        return True

    monkeypatch.setattr(app, "_canary_reads", canary)
    monkeypatch.setattr(
        app.store, "run_statement", _failing("Code: 497. DB::Exception: Not enough privileges")
    )
    problem, repairable = asyncio.run(app._probe())
    assert "lacks its grants on" in problem
    assert repairable


def test_a_foreign_permissive_policy_stops_the_repair(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written: list[list[str]] = []

    async def probe() -> tuple[str, bool]:
        return "memory admits rows in the reserved scope", True

    async def yes(*_: Any, **__: Any) -> bool:
        return True

    async def foreign(*_: Any, **__: Any) -> list[str]:
        return ["bi_scope ON memory"]

    async def none(*_: Any, **__: Any) -> None:
        return None

    async def write(statements: list[str], **_: Any) -> None:
        written.append(statements)

    class _Lease:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(app, "_probe", probe)
    monkeypatch.setattr(app, "lease", _Lease)
    monkeypatch.setattr(app.store, "sealed_secrets", none)
    monkeypatch.setattr(app.store, "can_manage_access", yes)
    monkeypatch.setattr(app.store, "foreign_policies", foreign)
    monkeypatch.setattr(app.store, "write_entities", write)
    asyncio.run(app.reprobe())
    assert not written
    ready_now, problem = app.analysis_state()
    assert ready_now is False
    assert "bi_scope ON memory" in problem


def test_ch_setup_refuses_a_new_key_beside_a_live_analyst_user(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    async def none(*_: Any, **__: Any) -> None:
        return None

    async def no(*_: Any, **__: Any) -> bool:
        return False

    class _Lease:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(app, "lease", _Lease)
    monkeypatch.setattr(app.store, "sealed_secrets", none)
    monkeypatch.setattr(app.store, "analyst_user_absent", no)
    with pytest.raises(RuntimeError, match=f"DROP USER {boundary.ANALYST_USER}"):
        asyncio.run(app.rewrite())
