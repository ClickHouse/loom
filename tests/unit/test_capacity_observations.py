"""Capacity diagnostics report one authorized scope without inventing missing evidence."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server import health
from server.auth import authz, tenancy
from server.auth.jwt import mint_token
from server.core import ch as ch_module
from server.core import pg, ratelimit, readiness
from server.core.schema import status as schema_status
from server.settings import Settings, settings
from tests.unit.test_health_placement_capacity import _headers
from tests.unit.test_health_placement_capacity import router as router

_CLIENT = httpx.AsyncClient
_NOW = datetime(2026, 1, 1, 12, 10, 30, tzinfo=UTC)
_MARK = _NOW - timedelta(seconds=90)
_END = _MARK.replace(second=0)


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.heads = 8
        self.vectors = 12
        self.mark: datetime | None = _MARK
        self.head_error: Exception | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((sql, dict(params or {})))
        if "sli_minute" in sql:
            if self.entered is not None:
                self.entered.set()
                assert self.release is not None
                await self.release.wait()
            return {
                "ingested_hwm": self.mark,
                "observed_at": _NOW,
                "window_start": _END - timedelta(minutes=5),
                "window_end": _END,
                "single_requests": 300,
                "bulk_requests": 60,
            }
        if "FROM loom.memory FINAL" in sql:
            if self.head_error is not None:
                raise self.head_error
            return {"n": self.heads, "observed_at": _NOW}
        assert "FROM loom.memory_embeddings" in sql, sql
        return {"n": self.vectors, "observed_at": _NOW}


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> _CH:
    ch = _CH()
    monkeypatch.setattr(ch_module, "get_client", lambda: ch)
    monkeypatch.setattr(
        ratelimit,
        "get_limiter",
        lambda: SimpleNamespace(
            check=lambda *args, **kwargs: SimpleNamespace(allowed=True, retry_after_s=0),
        ),
    )
    monkeypatch.setattr(tenancy, "is_held", AsyncMock(return_value=True))
    monkeypatch.setattr(authz, "check_authorized", AsyncMock())
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    return ch


@pytest.fixture
def app(router: Any, observed: _CH, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(health, "_ping_clickhouse", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(health, "_otel_unindexed_for_health", AsyncMock(return_value=None))
    monkeypatch.setattr(
        health,
        "_health_provider_fields",
        lambda *_a, **_kw: {"status": "ok", "fake_embedding": True},
    )
    monkeypatch.setattr(health, "_calibration_block", lambda *_a: {})
    monkeypatch.setattr(health, "_ratelimit_block", lambda: {})
    monkeypatch.setattr(health, "_deployment_config_block", lambda: {})
    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(schema_status, "schema_status", AsyncMock(return_value={}))
    monkeypatch.setattr(
        readiness, "evaluate", AsyncMock(return_value=readiness.Readiness({}, frozenset()))
    )
    app = FastAPI()
    health.attach(app)
    return app


def _capacity(app: FastAPI, query: str = "", *, headers: dict[str, str] | None = None) -> Any:
    response = TestClient(app).get(
        "/v1/health?include=capacity" + query,
        headers=_headers() if headers is None else headers,
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()["capacity"]


def _selected(app: FastAPI, *, headers: dict[str, str] | None = None) -> Any:
    answer = _capacity(app, "&capacity_org=org&capacity_namespace=private", headers=headers)
    assert "scope" in answer, "capacity must report the explicitly selected namespace"
    return answer["scope"]


def test_model_and_complete_window_keep_request_units_and_historical_costs(
    app: FastAPI,
    observed: _CH,
) -> None:
    answer = _capacity(app)
    assert "model" in answer, "capacity needs labelled historical storage baselines"
    assert answer["model"]["clickhouse"] == 13_600
    assert answer["model"]["postgres"] == 6_800
    assert answer["model"]["unit"] == "bytes_per_key"
    assert "dimensions" in answer["model"]["caveat"] and "estimate" in answer["model"]["caveat"]
    rates = answer["write_requests"]
    assert rates["status"] == "known"
    assert rates["unit"] == "successful_requests_per_second"
    assert rates["single"] == 1 and rates["bulk"] == 0.2
    assert rates["single_requests"] == 300 and rates["bulk_requests"] == 60
    assert rates["committed_rows_per_second"] is None
    assert rates["window_start"] == (_END - timedelta(minutes=5)).isoformat()
    assert rates["window_end"] == _END.isoformat() and rates["ingested_hwm"] == _MARK.isoformat()
    assert rates["lag_s"] == 90
    assert answer["scope"] == {"status": "not_requested"}
    assert len(observed.calls) == 1


@pytest.mark.parametrize(
    "count,status,flag",
    [
        (0, "exact", False),
        (999_999, "exact", False),
        (1_000_000, "exact", True),
        (1_000_001, "at_least", True),
    ],
)
def test_one_scope_has_separate_bounded_live_and_physical_vector_counts(
    app: FastAPI,
    observed: _CH,
    count: int,
    status: str,
    flag: bool,
) -> None:
    observed.heads = count
    scope = _selected(app)
    assert scope["org"] == "org" and scope["namespace"] == "private"
    assert scope["live_heads"]["value"] == count and scope["live_heads"]["status"] == status
    assert scope["live_heads"]["at_or_above_scan_policy"] is flag
    assert scope["vector_physical_rows"]["value"] == 12
    assert scope["vector_physical_rows"]["source"] == "clickhouse.memory_embeddings"
    assert scope["live_heads"]["source"] == "clickhouse.memory FINAL"
    assert scope["live_heads"]["observed_at"] == _NOW.isoformat()
    assert scope["live_heads"]["read_s"] >= 0
    for sql, params in observed.calls[1:]:
        assert params["org"] == "org" and params["ns"] == "private" and params["cap"] == 1_000_001
        assert "max_rows_to_read = 1000000" in sql and "max_memory_usage = 67108864" in sql
        assert "max_execution_time = 2" in sql and "read_overflow_mode = 'throw'" in sql
        assert "LIMIT {cap:UInt32}" in sql
    assert "tombstoned_at IS NULL" in observed.calls[1][0]
    assert "status IN ('active', 'conflicted')" in observed.calls[1][0]
    assert " FINAL" not in observed.calls[2][0]


@pytest.mark.parametrize("mark", [None, _NOW - timedelta(minutes=6)])
def test_missing_or_stale_rollup_never_reports_zero(app: FastAPI, observed: _CH, mark: Any) -> None:
    observed.mark = mark
    answer = _capacity(app)
    assert "write_requests" in answer, "missing rollup evidence must be explicit"
    assert answer["write_requests"]["status"] == "unknown"
    assert answer["write_requests"]["value"] is None
    assert "single" not in answer["write_requests"] and "bulk" not in answer["write_requests"]


def test_scan_budget_failure_is_unknown_while_independent_vector_count_survives(
    app: FastAPI,
    observed: _CH,
) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    observed.head_error = DatabaseError("Code: 158. private-store-secret TOO_MANY_ROWS")
    scope = _selected(app)
    assert scope["live_heads"]["status"] == "unknown" and scope["live_heads"]["value"] is None
    assert scope["live_heads"]["reason"] == "scan_limit_hit"
    assert scope["live_heads"]["at_or_above_scan_policy"] is None
    assert scope["vector_physical_rows"]["value"] == 12
    assert "private-store-secret" not in json.dumps(scope)


@pytest.mark.parametrize("refusal", ["unheld", "restricted"])
def test_unheld_or_unauthorized_scope_is_not_counted(
    app: FastAPI,
    observed: _CH,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    if refusal == "unheld":
        monkeypatch.setattr(tenancy, "is_held", AsyncMock(return_value=False))
    else:
        monkeypatch.setattr(
            authz, "check_authorized", AsyncMock(side_effect=PermissionError("private"))
        )
    scope = _selected(app)
    assert scope["status"] == "unknown" and scope["value"] is None
    assert "org" not in scope and "namespace" not in scope
    assert len(observed.calls) == 1 and "sli_minute" in observed.calls[0][0]


def test_pinned_admin_token_retains_its_own_user_read_scope(app: FastAPI, observed: _CH) -> None:
    token = mint_token(
        subject="capacity-reader",
        org="org",
        namespaces=["*"],
        roles=["platform_admin"],
        read_user_id="one-user",
    )
    scope = _selected(app, headers={"Authorization": "Bearer " + token})
    assert scope["read_scope"] == "own_user"
    for sql, params in observed.calls[1:]:
        assert "user_id = {scope_uid:String}" in sql and params["scope_uid"] == "one-user"


def test_postgres_authority_counts_current_heads_with_bounded_read_only_session(
    app: FastAPI,
    observed: _CH,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[tuple[str, tuple[Any, ...]]] = []

    class Connection:
        @asynccontextmanager
        async def transaction(self) -> Any:
            yield

        async def execute(self, sql: str) -> None:
            assert sql in {"SET LOCAL transaction_read_only = on", "SET LOCAL work_mem = '4MB'"}
            statements.append((sql, ()))

        async def fetchrow(self, sql: str, *args: Any) -> Any:
            statements.append((sql, args))
            return {"n": 5, "observed_at": _NOW}

    @asynccontextmanager
    async def dedicated(**kwargs: Any) -> Any:
        assert kwargs == {"statement_timeout_ms": 2000}
        yield Connection()

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(health, "_pg_ok", AsyncMock(return_value=True))
    token = mint_token(
        subject="capacity-reader",
        org="org",
        namespaces=["*"],
        roles=["platform_admin"],
        read_user_id="one-user",
    )
    scope = _selected(app, headers={"Authorization": "Bearer " + token})
    assert scope["live_heads"]["value"] == 5
    assert scope["live_heads"]["source"] == "postgres.memory_current"
    assert len(statements) == 3
    sql, args = statements[-1]
    assert "FROM loom.memory_current WHERE org = $1 AND namespace = $2" in sql
    assert "row->>'tombstoned_at' IS NULL" in sql and "row->>'status' = ANY($4::text[])" in sql
    assert "user_id = $5" in sql and "LIMIT $3" in sql
    assert args == ("org", "private", 1_000_001, ["active", "conflicted"], "one-user")
    assert len(observed.calls) == 2
    assert all("FROM loom.memory FINAL" not in text for text, _ in observed.calls)


@pytest.mark.parametrize(
    "query",
    [
        "&capacity_org=org",
        "&capacity_org=*&capacity_namespace=private",
        "&capacity_org=org&capacity_namespace=*",
    ],
)
def test_partial_or_wildcard_selector_does_not_enumerate_scopes(
    app: FastAPI,
    observed: _CH,
    query: str,
) -> None:
    answer = _capacity(app, query)
    assert "scope" in answer, "invalid selection must be explicit rather than ignored"
    assert answer["scope"]["status"] == "unknown"
    assert len(observed.calls) == 1


@pytest.mark.parametrize("caller", ["anonymous", "invalid", "writer", "org_admin", "scoped"])
def test_existing_positive_admin_boundary_blocks_all_observation_io(
    app: FastAPI,
    observed: _CH,
    caller: str,
) -> None:
    headers = (
        {}
        if caller == "anonymous"
        else {"Authorization": "Bearer invalid"}
        if caller == "invalid"
        else _headers([caller] if caller != "scoped" else None, scoped=caller == "scoped")
    )
    answer = TestClient(app).get(
        "/v1/health?include=capacity&capacity_org=org&capacity_namespace=private", headers=headers
    )
    assert answer.status_code == 200 and "capacity" not in answer.json()
    assert observed.calls == []


def test_ordinary_health_does_not_read_scope_or_rates(app: FastAPI, observed: _CH) -> None:
    answer = TestClient(app).get("/v1/health", headers=_headers())
    assert answer.status_code == 200 and "capacity" not in answer.json()
    assert observed.calls == []


@pytest.mark.asyncio
async def test_expensive_observation_is_immediate_busy_and_recovers_after_cancellation(
    app: FastAPI,
    observed: _CH,
) -> None:
    observed.entered, observed.release = asyncio.Event(), asyncio.Event()
    async with _CLIENT(
        transport=httpx.ASGITransport(app=app), base_url="http://cell.test"
    ) as client:
        first = asyncio.create_task(client.get("/v1/health?include=capacity", headers=_headers()))
        entered = asyncio.create_task(observed.entered.wait())
        try:
            done, _ = await asyncio.wait(
                {first, entered}, timeout=1, return_when=asyncio.FIRST_COMPLETED
            )
            assert entered in done, "capacity must reach the bounded telemetry read"
            second = await asyncio.wait_for(
                client.get("/v1/health?include=capacity", headers=_headers()), timeout=1
            )
            assert second.json()["capacity"]["write_requests"]["status"] == "unknown"
            assert len(observed.calls) == 1
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            observed.release.set()
            third = await client.get("/v1/health?include=capacity", headers=_headers())
            assert third.json()["capacity"]["write_requests"]["status"] == "known"
            assert len(observed.calls) == 2
        finally:
            observed.release.set()
            for task in (first, entered):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, entered, return_exceptions=True)


def test_doctor_consumes_same_request_units_and_one_scope(
    observed: _CH,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from server.cli import _ch, doctor

    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(doctor, "_run_doctor_checks", lambda *_a: [])
    monkeypatch.setattr(doctor, "_doctor_problems", lambda: [])
    monkeypatch.setattr(_ch, "connect_declared", AsyncMock())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock())
    args = argparse.Namespace(
        capacity=True, capacity_org="org", capacity_namespace="private", json=True
    )
    result = doctor._cmd_doctor(args)
    answer = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in answer["checks"]}
    assert "capacity_write_requests" in checks, "doctor must consume the capacity reader"
    assert result == 0 and checks["capacity_placement"]["grade"] == "WARN"
    assert json.loads(checks["capacity_write_requests"]["detail"])["bulk"] == 0.2
    selected = json.loads(checks["capacity_scope"]["detail"])
    assert selected["org"] == "org" and selected["namespace"] == "private"
    assert selected["live_heads"]["value"] == 8 and selected["vector_physical_rows"]["value"] == 12
    assert len(observed.calls) == 3


def test_doctor_rejects_partial_scope_without_reading_stores(
    observed: _CH,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from server.cli import doctor

    monkeypatch.setattr(doctor, "_run_doctor_checks", lambda *_a: [])
    monkeypatch.setattr(doctor, "_doctor_problems", lambda: [])
    result = doctor._cmd_doctor(
        argparse.Namespace(
            capacity=True,
            capacity_org="org",
            capacity_namespace=None,
            json=True,
        )
    )
    answer = json.loads(capsys.readouterr().out)
    assert result == 1, "doctor must reject a partial explicit scope"
    assert answer["checks"][0]["name"] == "capacity_scope"
    assert observed.calls == []


@pytest.mark.asyncio
async def test_operator_capacity_projection_reuses_existing_row_without_rotation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

    from server.router import app as router_app
    from server.router import directory

    row = SimpleNamespace(closed=True, placements_max=None)
    found = SimpleNamespace(row=row, other=None)
    read = AsyncMock(return_value=found)
    monkeypatch.setattr(directory, "attach_read", read)
    monkeypatch.setattr(router_app.Router, "_operator", lambda *_a: None)
    monkeypatch.setattr(
        router_app, "_attach_answer", lambda _found: {"cell": "eu-1", "placed_orgs": 7}
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/cells/v1/operator/cells/eu-1",
            "headers": [],
            "path_params": {"cell": "eu-1"},
        }
    )
    response = await router_app.Router().operator_cell(request)
    answer = json.loads(response.body)
    assert "placements_max" in answer, "doctor's existing operator read must expose its quota"
    assert (
        response.status_code == 200
        and answer["closed"] is True
        and answer["placements_max"] is None
    )
    assert answer["placed_orgs"] == 7
    assert "next_credential_pending" not in answer and "credential_sha256" not in answer
    read.assert_awaited_once_with("eu-1", audited=True)


@pytest.mark.parametrize(
    ("name", "valid", "invalid"),
    [
        (
            "capacity_rollup_max_lag_s",
            (0.25, 300.0),
            (0, -1, float("nan"), float("inf"), float("-inf")),
        ),
        (
            "capacity_observation_timeout_s",
            (0.25, 3.0),
            (0, -1, float("nan"), float("inf"), float("-inf")),
        ),
        ("capacity_scope_max_rows", (1, 1_000_000), (0, -1, 1_000_001)),
        (
            "capacity_read_max_execution_s",
            (0.001, 2.0),
            (0, -1, 0.0009, float("nan"), float("inf"), float("-inf")),
        ),
        ("capacity_read_max_bytes", (1, 67_108_864), (0, -1)),
        ("capacity_pg_work_mem_mib", (1, 4), (0, -1)),
    ],
)
def test_capacity_settings_require_bounded_positive_budgets(
    name: str,
    valid: tuple[int | float, ...],
    invalid: tuple[int | float, ...],
) -> None:
    for value in valid:
        loaded = Settings(_env_file=None, **{name: value})
        assert getattr(loaded, name) == value
    for value in invalid:
        with pytest.raises(ValidationError, match=name):
            Settings(_env_file=None, **{name: value})
