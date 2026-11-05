"""Caller-shaped reads run under ceilings a request cannot widen, and every
query of a request carries the verified caller's quota key."""

from __future__ import annotations

import contextvars
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import server.core.ch as ch
from server.auth.bearer import verify_bearer_header
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo, mint_token
from server.core.errors import ValidationError
from server.operations.base import Role
from server.settings import settings

_SELECT = "SELECT path FROM loom.code_chunks WHERE match(content, {p:String})"


@pytest.fixture(autouse=True)
def _ceilings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_caller_shaped_max_execution_s", 30.0)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_memory_bytes", 4_000_000_000)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_rows_to_read", 500_000_000)


def _isolated(fn: Any) -> Any:
    """Run `fn` in a copy of the context, so a context var it sets does not leak."""
    return contextvars.copy_context().run(fn)


def _caller_shaped_settings(sql: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    with ch.caller_shaped():
        return ch.statement_settings(sql, extra)


def test_a_caller_shaped_read_carries_the_ceilings() -> None:
    out = _caller_shaped_settings(_SELECT)
    assert out["max_execution_time"] == 30
    assert out["max_memory_usage"] == 4_000_000_000
    assert out["max_rows_to_read"] == 500_000_000
    assert out["read_overflow_mode"] == "throw"


def test_a_per_call_value_cannot_widen_a_ceiling_but_may_lower_it() -> None:
    wider = {"max_execution_time": 3600, "max_memory_usage": 0, "max_rows_to_read": 10**12}
    out = _caller_shaped_settings(_SELECT, {**wider, "read_overflow_mode": "break"})
    assert out["max_execution_time"] == 30
    assert out["max_memory_usage"] == 4_000_000_000  # 0 is unlimited in ClickHouse
    assert out["max_rows_to_read"] == 500_000_000
    assert out["read_overflow_mode"] == "throw"
    assert _caller_shaped_settings(_SELECT, {"max_execution_time": 2})["max_execution_time"] == 2


def test_a_statement_timeout_scope_cannot_widen_it() -> None:
    with ch.statement_timeout(3600):
        assert _caller_shaped_settings(_SELECT)["max_execution_time"] == 30


def test_reads_outside_the_scope_and_writes_inside_it_are_untouched() -> None:
    out = ch.statement_settings(_SELECT)
    assert "max_rows_to_read" not in out and "max_memory_usage" not in out
    insert = _caller_shaped_settings("INSERT INTO loom.code_traces VALUES")
    assert "max_rows_to_read" not in insert and "read_overflow_mode" not in insert


@pytest.mark.parametrize(
    "err",
    [
        RuntimeError("Code: 158. DB::Exception: Limit for rows exceeded"),
        # captured from ClickHouse 26.5.1.882 with max_memory_usage=5000000
        RuntimeError(
            "Code: 241. DB::Exception: Query memory limit exceeded: would use 7.61 MiB "
            "(attempt to allocate chunk of 6.00 MiB), maximum: 4.77 MiB: While executing "
            "AggregatingTransform. (MEMORY_LIMIT_EXCEEDED) (version 26.5.1.882 (official build))"
        ),
        RuntimeError("Code: 159. DB::Exception: Timeout exceeded: elapsed 30.1 seconds"),
    ],
)
def test_a_row_or_memory_cap_hit_inside_the_scope_is_a_422(err: RuntimeError) -> None:
    with ch.caller_shaped(), pytest.raises(ValidationError, match="narrow it"):
        ch._raise_if_caller_cap(err)
    ch._raise_if_caller_cap(err)  # outside the scope: left to the caller as before


def test_a_cap_hit_says_how_the_caller_narrows_its_own_request() -> None:
    err = RuntimeError("Code: 158. DB::Exception: Limit for rows exceeded")
    with ch.caller_shaped(narrow="fewer datasets"), pytest.raises(ValidationError) as e:
        ch._raise_if_caller_cap(err)
    assert str(e.value).endswith("narrow it (fewer datasets)")
    with ch.caller_shaped(), pytest.raises(ValidationError, match=r"\(a path prefix or globs"):
        ch._raise_if_caller_cap(err)  # the hint does not outlive its scope


@pytest.mark.parametrize(
    "err",
    [
        # captured from ClickHouse 26.5.1.882 with max_server_memory_usage=1000000000
        RuntimeError(
            "Code: 241. DB::Exception: (total) memory limit exceeded: would use 1000.04 MiB "
            "(attempt to allocate chunk of 512.00 MiB), current RSS: 702.20 MiB, maximum: "
            "953.67 MiB. OvercommitTracker decision: Query was selected to stop by "
            "OvercommitTracker: While executing AggregatingTransform. (MEMORY_LIMIT_EXCEEDED) "
            "(version 26.5.1.882 (official build))"
        ),
        # captured from ClickHouse 26.5.1.882 with max_memory_usage_for_user=5000000
        RuntimeError(
            "Code: 241. DB::Exception: User memory limit exceeded: would use 6.59 MiB "
            "(attempt to allocate chunk of 6.00 MiB), maximum: 4.77 MiB. OvercommitTracker "
            "decision: Query was selected to stop by OvercommitTracker: While executing "
            "AggregatingTransform. (MEMORY_LIMIT_EXCEEDED) (version 26.5.1.882 (official build))"
        ),
    ],
)
def test_the_server_or_user_memory_limit_stays_a_server_error(err: RuntimeError) -> None:
    with ch.caller_shaped():
        ch._raise_if_caller_cap(err)


def _principal(org: str, subject: str) -> AuthInfo:
    return AuthInfo(
        subject=subject, org=org, namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )


def test_every_query_of_a_request_carries_the_tokens_quota_key() -> None:
    def _bound() -> tuple[dict[str, Any], dict[str, Any]]:
        set_current_auth(_principal("acme", "alice"))
        # a body-derived or per-call value never replaces the token's key
        return ch.statement_settings(_SELECT, {"quota_key": "victim:bob"}), ch.query_settings()

    read, insert = _isolated(_bound)
    assert read["quota_key"] == "acme:alice"
    assert insert["quota_key"] == "acme:alice"


def test_no_principal_no_quota_key() -> None:
    def _anonymous() -> dict[str, Any]:
        set_current_auth(_principal("acme", "alice"))
        set_current_auth(None)
        return ch.statement_settings(_SELECT)

    assert "quota_key" not in _isolated(_anonymous)


def test_a_route_outside_the_registry_carries_the_quota_key_once_the_bearer_verifies() -> None:
    # /v1/me/*, downloads and the admin routes verify through server.auth.bearer
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, Any]:
        verify_bearer_header(request.headers.get("Authorization"))
        return {"key": ch.statement_settings(_SELECT).get("quota_key")}

    tok = mint_token(subject="alice", org="acme", namespaces=["n"], roles=["viewer"])
    r = TestClient(app).get("/probe", headers={"Authorization": f"Bearer {tok}"})
    assert r.json() == {"key": "acme:alice"}


class _ScopeSpy:
    """A ClickHouse client that records whether each read ran caller-shaped."""

    def __init__(self) -> None:
        self.shaped: list[bool] = []

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        self.shaped.append(ch.caller_shaped_var.get())
        return []


@pytest.mark.asyncio
async def test_memory_aggregate_and_record_query_read_caller_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_records import MemoryRecordQueryInput
    from server.memory.api.models_search import MemoryAggregateInput
    from server.memory.app import query as query_mod
    from server.memory.app import records as records_mod

    spy = _ScopeSpy()
    monkeypatch.setattr(query_mod, "get_client", lambda: spy)
    monkeypatch.setattr(records_mod, "get_client", lambda: spy)
    await query_mod.handle_memory_aggregate(MemoryAggregateInput(org="o", namespace="n", agent="a"))
    await records_mod.handle_memory_record_query(
        MemoryRecordQueryInput(org="o", namespace="n", schema_name="s")
    )
    assert spy.shaped == [True, True]
    assert ch.caller_shaped_var.get() is False  # nothing leaks out of the handler


@pytest.mark.asyncio
async def test_code_grep_and_a_patterned_code_search_run_caller_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.api.models_search import CodeSearchInput
    from server.code.app import grep as grep_mod
    from server.code.app.search import handler as search_mod

    seen: list[bool] = []

    async def _record(_input: Any) -> Any:
        seen.append(ch.caller_shaped_var.get())

    monkeypatch.setattr(grep_mod, "_code_grep", _record)
    monkeypatch.setattr(search_mod, "_code_search", _record)
    await grep_mod.handle_code_grep(CodeGrepInput(pattern="ab"))
    await search_mod.handle_code_search(CodeSearchInput(query="q", pattern="ab"))
    await search_mod.handle_code_search(CodeSearchInput(query="q", path_globs=["src/**"]))
    await search_mod.handle_code_search(CodeSearchInput(query="q"))
    assert seen == [True, True, True, False]


class _RecordingDriver:
    """The driver under CHClient: records the extra keyword arguments of each call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def query(self, _sql: str, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("R", (), {"result_rows": [], "column_names": [], "summary": {}})()

    async def command(self, _sql: str, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_only_caller_shaped_reads_go_as_the_reader_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    monkeypatch.setattr(settings, "ch_password", "pw")
    driver = _RecordingDriver()
    client = ch.CHClient.__new__(ch.CHClient)
    client._client = driver  # type: ignore[assignment]
    await client.query_all(_SELECT)
    with ch.caller_shaped():
        await client.query_all(_SELECT)
        await client.execute("INSERT INTO loom.code_traces VALUES")
    outside, shaped, write = driver.calls
    assert "transport_settings" not in outside and "transport_settings" not in write
    auth = shaped["transport_settings"]["Authorization"]
    assert base64.b64decode(auth.removeprefix("Basic ")).decode() == "loom_reader:pw"


@pytest.mark.parametrize("sql", ["(SELECT 1) UNION ALL (SELECT 2)", "EXPLAIN SELECT 1"])
async def test_a_read_as_the_reader_user_always_carries_the_ceilings(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    driver = _RecordingDriver()
    client = ch.CHClient.__new__(ch.CHClient)
    client._client = driver  # type: ignore[assignment]
    with ch.caller_shaped():
        await client.query_all(sql)
    (call,) = driver.calls
    assert "transport_settings" in call
    assert call["settings"]["max_rows_to_read"] == 500_000_000
    assert call["settings"]["read_overflow_mode"] == "throw"


def test_no_reader_user_means_no_auth_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_reader_user", "")
    with ch.caller_shaped():
        assert ch._reader_auth() == {}


class _RefusingClient:
    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError(
            "Code: 516. DB::Exception: loom_reader: Authentication failed: password is "
            "incorrect, or there is no user with such name. (AUTHENTICATION_FAILED)"
        )


class _ReaderClient:
    async def query_one(self, sql: str, *_: Any, **__: Any) -> dict[str, Any]:
        assert ch.caller_shaped_var.get()  # so the read goes out as the reader user
        assert "FROM loom.memory" in sql  # so a missing grant fails the probe
        return {"u": "loom_reader", "n": 0}


async def test_a_reader_user_clickhouse_refuses_is_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability.problems import HealthState, collect_problems

    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    monkeypatch.setattr(ch, "_reader_user_ok", None)
    monkeypatch.setattr(ch, "_reader_user_error", "")
    await ch.probe_reader_user(_RefusingClient())
    assert ch.reader_user_state() is False
    assert "AUTHENTICATION_FAILED" in ch.reader_user_error()
    codes = [p.code for p in collect_problems(settings, HealthState(ch_reader_ok=False))]
    assert "ch_reader_user_failing" in codes

    await ch.probe_reader_user(_ReaderClient())
    assert ch.reader_user_state() is True
    assert ch.reader_user_error() == ""
    codes = [p.code for p in collect_problems(settings, HealthState(ch_reader_ok=True))]
    assert "ch_reader_user_failing" not in codes


def test_a_ceiling_above_the_reader_profile_names_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability.problems import HealthState, collect_problems

    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    # captured from ClickHouse 26.5.1.882 as loom_reader with max_execution_time=31
    err = (
        "Code: 452. DB::Exception: Setting max_execution_time shouldn't be greater than 30. "
        "(SETTING_CONSTRAINT_VIOLATION) (version 26.5.1.882 (official build))"
    )
    state = HealthState(ch_reader_ok=False, ch_reader_error=err)
    (problem,) = [
        p for p in collect_problems(settings, state) if p.code == "ch_reader_user_failing"
    ]
    assert "max_execution_time shouldn't be greater than 30" in problem.fix
    assert "LOOM_CH_CALLER_SHAPED_" in problem.fix and "Install" not in problem.fix


async def test_no_reader_user_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_reader_user", "")
    monkeypatch.setattr(ch, "_reader_user_ok", None)
    await ch.probe_reader_user(_RefusingClient())
    assert ch.reader_user_state() is None


def test_the_doctor_reports_a_refused_reader_user(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from server.cli import doctor

    sent: list[httpx.Request] = []

    def _refuse(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(516, text="Code: 516. AUTHENTICATION_FAILED")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(_refuse), **kw)
    )
    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    assert doctor._doctor_reader_probe(settings) == ("failing", "Code: 516. AUTHENTICATION_FAILED")
    assert "ch_reader_user_failing" in [p.code for p in doctor._doctor_problems()]
    # the boot probe's read, under the caps the caller-shaped reads carry
    assert b"FROM loom.memory" in sent[0].content
    assert sent[0].url.params["max_execution_time"] == "30"
    assert sent[0].url.params["max_rows_to_read"] == "500000000"


# captured from ClickHouse 26.5.1.882 as loom_reader before migrations
_NO_TABLE = (
    "Code: 60. DB::Exception: Unknown table expression identifier 'loom.memory' in scope "
    "SELECT 1 FROM loom.memory LIMIT 0. (UNKNOWN_TABLE) (version 26.5.1.882 (official build))"
)


class _NoTableClient:
    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError(_NO_TABLE)


async def test_before_migrations_the_boot_probe_reports_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    monkeypatch.setattr(ch, "_reader_user_ok", None)
    monkeypatch.setattr(ch, "_reader_user_error", "")
    await ch.probe_reader_user(_NoTableClient())
    assert ch.reader_user_state() is None


def test_before_migrations_the_doctor_does_not_blame_the_reader_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from server.cli import doctor

    real_client = httpx.Client
    no_table = httpx.MockTransport(lambda _: httpx.Response(404, text=_NO_TABLE))
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=no_table, **kw))
    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    assert doctor._doctor_reader_probe(settings) == (
        "not_probed",
        "reader user not probed: loom.memory missing",
    )
    lines = doctor._problem_checks(doctor._doctor_problems())
    assert "problem:ch_reader_user_failing" not in [name for name, _, _ in lines]
    (line,) = [ln for ln in lines if ln[0] == "problem:ch_reader_user_not_probed"]
    assert line[1:] == (
        doctor.PASS,
        "reader user not probed: loom.memory missing Fix: run `loom migrate`.",
    )


def test_the_doctor_does_not_probe_an_unset_reader_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor

    monkeypatch.setattr(settings, "ch_reader_user", "")
    assert doctor._doctor_reader_probe(settings) == ("not_probed", "")
    codes = [p.code for p in doctor._doctor_problems()]
    assert "ch_reader_user_not_probed" not in codes
    assert "ch_reader_user_failing" not in codes


def test_a_ceiling_below_the_reader_profile_is_not_advised_lower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability.problems import HealthState, collect_problems

    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    err = (
        "Code: 452. DB::Exception: Setting max_rows_to_read shouldn't be less than 1. "
        "(SETTING_CONSTRAINT_VIOLATION) (version 26.5.1.882 (official build))"
    )
    state = HealthState(ch_reader_ok=False, ch_reader_error=err)
    (problem,) = [
        p for p in collect_problems(settings, state) if p.code == "ch_reader_user_failing"
    ]
    assert "max_rows_to_read shouldn't be less than 1" in problem.fix
    assert "outside the loom_reader profile's bounds" in problem.fix
    assert "Lower it" not in problem.fix
