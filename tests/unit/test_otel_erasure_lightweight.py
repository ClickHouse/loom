"""The ClickStack erasure leg masks rows with a lightweight
DELETE instead of rewriting parts, and `loom doctor` names the OTel metrics
tables that lack the `mapValues(Attributes)` bloom filter the predicate
needs. CH-free."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.cli import doctor
from server.erasure.app import backends as eb
from server.erasure.store import health as erasure_health
from server.erasure.store import mutations as erasure_mutations
from server.observability.problems import HealthState, collect_problems


class _CH:
    def __init__(self, n: int = 3) -> None:
        self.n = n
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        return {"n": self.n}

    async def execute(self, sql, params=None):
        self.commands.append((" ".join(sql.split()), dict(params or {})))

    async def query_all(self, sql, params=None):
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


@pytest.fixture
def ch(monkeypatch):
    import server.core.ch as chmod

    c = _CH()
    monkeypatch.setattr(chmod, "get_client", lambda: c)
    monkeypatch.setattr(erasure_mutations, "_default_client", lambda: c)
    return c


@pytest.mark.asyncio
async def test_every_otel_leg_is_a_lightweight_delete_with_the_saga_tag(ch) -> None:
    backend = eb.ClickStackBackend(database="default", timeout_s=5.0)
    n = await backend.delete_namespace("org", "ns-1")
    assert n == 3 * len(backend.DEFAULT_TABLES) and len(ch.commands) == len(backend.DEFAULT_TABLES)
    for sql, params in ch.commands:
        assert sql.startswith("DELETE FROM default.otel_") or sql.startswith(
            "DELETE FROM default.hyperdx_sessions"
        )
        assert "ALTER TABLE" not in sql and "lightweight_deletes_sync = 0" in sql
        assert "['loom.namespace'] = {u:String}" in sql and params["u"] == "ns-1"
        assert params["erasure_mutation_tag"].startswith("erasure:")
    # the user scope takes the same path
    ch.commands.clear()
    await backend.delete_user_id("org", "alice")
    assert all(
        s.startswith("DELETE FROM") and "['user_id'] = {u:String}" in s for s, _ in ch.commands
    )


@pytest.mark.asyncio
async def test_a_zero_count_issues_nothing(ch) -> None:
    ch.n = 0
    backend = eb.ClickStackBackend(database="default", timeout_s=5.0)
    assert await backend.delete_namespaces("org", ["a", "b"]) == 0 and ch.commands == []


def _settings(db: str = "default") -> Any:
    return SimpleNamespace(
        clickstack_database=db,
        ch_host="h",
        ch_port=8443,
        ch_secure=None,
        ch_user="u",
        ch_password="p",
    )


def test_doctor_warns_on_metrics_tables_without_the_value_index(monkeypatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def _q(settings, sql, params):
        seen.append((sql, params))
        return [
            {"name": "otel_metrics_gauge", "n": 0},
            {"name": "otel_metrics_histogram", "n": 0},
            {"name": "otel_metrics_sum", "n": 1},
        ]

    monkeypatch.setattr(doctor, "_doctor_ch_query", _q)
    name, grade, detail = doctor._doctor_otel_erasure_index_check(_settings())
    assert (name, grade) == ("otel_erasure_index", doctor.WARN)
    assert (
        "default.otel_metrics_gauge, otel_metrics_histogram" in detail
        and "ClickStack erasure index" in detail
    )
    assert "otel_metrics_sum" not in detail.split("have no")[0]
    ((sql, params),) = seen
    assert "system.data_skipping_indices" in sql and params["db"] == "default"
    assert set(params["tables"]) == set(erasure_health.OTEL_METRICS_TABLES)


def test_doctor_passes_when_every_present_table_is_indexed_or_the_leg_is_off(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor, "_doctor_ch_query", lambda s, q, p: [{"name": "otel_metrics_gauge", "n": 1}]
    )
    assert doctor._doctor_otel_erasure_index_check(_settings())[1] == doctor.PASS
    name, grade, detail = doctor._doctor_otel_erasure_index_check(_settings(db=""))
    assert grade == doctor.PASS and "unset" in detail


def test_doctor_read_failure_is_a_warn_not_a_crash(monkeypatch) -> None:
    def _boom(s, q, p):
        raise RuntimeError("timeout")

    monkeypatch.setattr(doctor, "_doctor_ch_query", _boom)
    name, grade, detail = doctor._doctor_otel_erasure_index_check(_settings())
    assert grade == doctor.WARN and "timeout" in detail


@pytest.mark.asyncio
async def test_the_probe_reads_once_an_hour_per_database() -> None:
    class _Q:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def query_all(self, sql, params=None):
            self.calls.append(dict(params or {}))
            assert "system.data_skipping_indices" in sql and "FORMAT" not in sql
            return [{"name": "otel_metrics_gauge", "n": 0}, {"name": "otel_metrics_sum", "n": 2}]

    q = _Q()
    erasure_health._OTEL_INDEX_CACHE.clear()
    out = await erasure_health.otel_metrics_unindexed("default", ch=q, now=1000.0)
    assert out == ("otel_metrics_gauge",)
    assert q.calls == [{"db": "default", "tables": list(erasure_health.OTEL_METRICS_TABLES)}]
    assert await erasure_health.otel_metrics_unindexed("default", ch=q, now=1000.0 + 3599) == out
    assert len(q.calls) == 1  # cached
    await erasure_health.otel_metrics_unindexed("default", ch=q, now=1000.0 + 3601)
    assert len(q.calls) == 2  # expired
    erasure_health._OTEL_INDEX_CACHE.clear()


def test_the_health_problem_names_the_tables_and_the_runbook() -> None:
    s = SimpleNamespace(clickstack_database="default")
    from server.observability import problems as pr

    out = pr._otel_problems(
        s, HealthState(otel_metrics_unindexed=("otel_metrics_gauge", "otel_metrics_sum"))
    )
    (p,) = out
    assert p.code == "otel_erasure_index" and p.severity == "warn" and p.source == "erasure"
    assert "default.otel_metrics_gauge, otel_metrics_sum" in p.message
    assert (
        "mapValues(Attributes)" in p.fix
        and "ClickStack erasure index" in p.fix
        and "MATERIALIZE" in p.fix
    )
    # unknown (not probed), indexed, or leg off → nothing
    assert pr._otel_problems(s, HealthState()) == []
    assert pr._otel_problems(s, HealthState(otel_metrics_unindexed=())) == []
    assert (
        pr._otel_problems(
            SimpleNamespace(clickstack_database=""), HealthState(otel_metrics_unindexed=("x",))
        )
        == []
    )


def test_collect_problems_carries_the_entry(monkeypatch) -> None:
    from server.settings import Settings

    monkeypatch.setenv("LOOM_CLICKSTACK_DATABASE", "default")
    s = Settings(_env_file=None)
    codes = [
        p.code
        for p in collect_problems(
            s, HealthState(env={}, otel_metrics_unindexed=("otel_metrics_histogram",))
        )
    ]
    assert "otel_erasure_index" in codes


def test_http_params_use_clickhouse_array_literals_not_json() -> None:
    """An Array(String) parameter sent as JSON gets 400 Bad Request: the HTTP
    interface reads ClickHouse's literal form."""
    assert doctor._ch_http_param(["otel_metrics_gauge", "otel_metrics_sum"]) == (
        "['otel_metrics_gauge','otel_metrics_sum']"
    )
    assert doctor._ch_http_param(("a'b",)) == "['a\\'b']"
    assert doctor._ch_http_param("default") == "default" and doctor._ch_http_param(3) == "3"


def test_doctor_http_read_carries_the_command_tag(monkeypatch) -> None:
    """The doctor's direct HTTP read is attributed like a CHClient query."""
    import httpx

    from server.core.ch import query_tag_var

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, text='{"name": "otel_metrics_sum", "n": 1}\n')

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    tag = query_tag_var.set("cli.doctor")
    try:
        rows = doctor._doctor_ch_query(_settings(), "SELECT 1", {"db": "default"})
    finally:
        query_tag_var.reset(tag)
    assert rows == [{"name": "otel_metrics_sum", "n": 1}]
    assert seen["log_comment"] == "cli.doctor" and seen["param_db"] == "default"
