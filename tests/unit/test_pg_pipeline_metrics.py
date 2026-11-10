"""The Postgres publication pipeline's metrics (`server.observability.pg_pipeline`)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.errors import NotFoundError
from server.core.pg import PostgresUnavailableError
from server.observability import pg_pipeline
from server.settings import settings
from tests.unit.clock_fake import ManualClock


class _Instrument:
    def __init__(self) -> None:
        self.points: list[tuple[float, dict[str, str]]] = []

    def record(self, value: float, attributes: dict[str, str]) -> None:
        self.points.append((value, attributes))

    def add(self, value: float, attributes: dict[str, str]) -> None:
        self.points.append((value, attributes))


def test_a_backlog_reading_is_dropped_after_two_export_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(pg_pipeline, "_readings", {})  # not the module's own
    monkeypatch.setattr(pg_pipeline, "time", clock)
    monkeypatch.setattr(settings, "otel_metrics_export_interval_s", 60)
    pg_pipeline.set_backlog("memory", pg_pipeline.Backlog(3, 0, 1.5))
    assert pg_pipeline.fresh_backlog()["memory"].pending == 3
    clock.advance(121)
    assert "memory" not in pg_pipeline.fresh_backlog()


def _fake_completion(
    monkeypatch: pytest.MonkeyPatch, locked: list[dict[str, Any]]
) -> list[tuple[float, dict[str, str]]]:
    from server.memory.store import pg_memory

    lag = _Instrument()
    monkeypatch.setattr(pg_pipeline, "_projection_lag", lag)

    class _Conn:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            assert "clock_timestamp() - r.committed_at" in sql
            return locked

        async def execute(self, sql: str, *args: Any) -> str:
            return "UPDATE 1"

    asyncio.run(
        pg_memory._complete_in(
            _Conn(), [("o", "n", 1)], pg_memory.SEARCH_DOC, pg_memory.COMMIT_TOKEN
        )
    )
    return lag.points


def test_projection_lag_is_recorded_when_the_last_search_target_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import pg_memory

    points = _fake_completion(
        monkeypatch,
        [
            # Its document lands, its row already did: searchable now.
            {
                "targets": pg_memory.SEARCH_DOC | pg_memory.AUDIT_ROW,
                "lease_token": 1,
                "lag_ms": 40.0,
            },
            # Its row is still owed: not yet.
            {"targets": pg_memory.SEARCHED, "lease_token": 1, "lag_ms": 50.0},
            # It owed no search target: nothing to say.
            {"targets": pg_memory.AUDIT_ROW, "lease_token": 1, "lag_ms": 60.0},
            # Its document lands through a redelivery.
            {"targets": pg_memory.SEARCH_DOC, "lease_token": 7, "lag_ms": 900.0},
        ],
    )
    assert points == [(40.0, {"path": "flush"}), (900.0, {"path": "redelivered"})]


@pytest.mark.parametrize(
    "behind, raises, outcome",
    [({}, False, None), ({"n": 5}, False, "passed"), ({"n": 5}, True, "deadline")],
)
def test_the_barrier_records_a_wait_only_when_something_was_behind(
    monkeypatch: pytest.MonkeyPatch, behind: dict[str, int], raises: bool, outcome: str | None
) -> None:
    from server.core import pg
    from server.memory.app.pg_admission import ProjectionBehindError
    from server.memory.app.search import pg_validation
    from server.memory.store import pg_memory

    waits = _Instrument()
    monkeypatch.setattr(pg_pipeline, "_search_wait", waits)
    monkeypatch.setattr(pg, "enabled", lambda: True)

    async def unsearchable(org: str, namespaces: Any, owed: int) -> dict[str, int]:
        return behind

    async def wait(*a: Any, **k: Any) -> None:
        if raises:
            raise ProjectionBehindError("behind")

    monkeypatch.setattr(pg_memory, "unsearchable_through", unsearchable)
    monkeypatch.setattr(pg_validation, "wait_searchable", wait)
    from server.observability import genai

    monkeypatch.setattr(genai, "current_operation", lambda: "memory.search")

    async def run() -> bool:
        return await pg_validation.admit("o", ["n"])

    if raises:
        with pytest.raises(ProjectionBehindError):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) is bool(behind)
    assert [p[1]["outcome"] for p in waits.points] == ([outcome] if outcome else [])
    assert all(p[1]["loom.operation"] == "memory.search" for p in waits.points)


def test_the_error_code_goes_on_the_request_counter_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import otel

    counter, duration = _Instrument(), _Instrument()
    monkeypatch.setattr(otel, "_request_counter", counter)
    monkeypatch.setattr(otel, "_request_duration", duration)
    otel.record_op(
        "memory.search",
        namespace="n",
        success=False,
        duration_ms=3.0,
        error_code="PROJECTION_BEHIND",
    )
    assert counter.points[0][1]["loom.error_code"] == "PROJECTION_BEHIND"
    assert "loom.error_code" not in duration.points[0][1]


def _readings_of(source_reads: list[str]) -> Any:
    def reading(source: str) -> Any:
        async def read(cap: int) -> pg_pipeline.Backlog:
            source_reads.append(source)
            return pg_pipeline.Backlog(1, 4, 2.0)

        return read

    return reading


def test_health_reads_the_backlog_itself_off_the_lease_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import health
    from server.knowledge.store import pg_documents
    from server.memory.store import pg_memory
    from server.observability import otel_probes

    reads: list[str] = []
    reading = _readings_of(reads)
    monkeypatch.setattr(otel_probes, "census_reads_here", lambda: False)
    monkeypatch.setattr(pg_memory, "backlog", reading("memory"))
    monkeypatch.setattr(pg_memory, "outbox_backlog", reading("audit_outbox"))
    monkeypatch.setattr(pg_documents, "backlog", reading("knowledge"))
    monkeypatch.setattr(health, "_backlog_cache", {"at": None, "value": None})
    first = asyncio.run(health._backlog())
    asyncio.run(health._backlog())  # within the cache period: no second reading
    # The parked counts are the block's own fields; the reading says how old it is.
    assert first["memory"] == {
        "pending": 1, "oldest_age_s": 2.0, "capped": False, "reading_age_s": 0.0,
    }  # fmt: skip
    assert reads == ["memory", "audit_outbox", "knowledge"]


def test_health_on_the_lease_holder_answers_its_fresh_readings_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import health
    from server.memory.store import pg_memory
    from server.observability import otel_probes

    async def never(cap: int) -> Any:
        raise AssertionError("read the backlog although the lease holder's is fresh")

    monkeypatch.setattr(otel_probes, "census_reads_here", lambda: True)
    monkeypatch.setattr(pg_pipeline, "_readings", {})
    for source in pg_pipeline.SOURCES:
        pg_pipeline.set_backlog(source, pg_pipeline.Backlog(3, 0, 1.0))
    monkeypatch.setattr(pg_memory, "backlog", never)
    got = asyncio.run(health._backlog())
    assert set(got) == set(pg_pipeline.SOURCES) and got["memory"]["pending"] == 3


def test_the_backlog_gauges_export_only_on_the_lease_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import otel_probes

    gauges: dict[str, Any] = {}

    class _Meter:
        def create_histogram(self, **kw: Any) -> Any:
            return _Instrument()

        def create_counter(self, **kw: Any) -> Any:
            return _Instrument()

        def create_observable_gauge(self, *, name: str, callbacks: list[Any], **kw: Any) -> None:
            gauges[name] = callbacks[0]

    # `register` sets the module's instruments: put them back after.
    for name in ("_search_wait", "_projection_lag", "_inserts", "_insert_rows"):
        monkeypatch.setattr(pg_pipeline, name, None)
    monkeypatch.setattr(pg_pipeline, "_readings", {})
    pg_pipeline.set_backlog("memory", pg_pipeline.Backlog(2, 1, 5.0))
    pg_pipeline.register(_Meter())
    for here, expected in ((False, 0), (True, 2)):
        monkeypatch.setattr(otel_probes, "census_reads_here", lambda here=here: here)
        assert len(gauges["loom.pg.backlog"](None)) == expected


def test_a_backlog_read_that_fails_is_logged_and_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.memory.store import pg_memory
    from server.memory.workers import pg_delivery

    async def down(cap: int) -> Any:
        raise ConnectionError("the authority is unreachable")

    monkeypatch.setattr(pg_memory, "backlog", down)
    asyncio.run(pg_delivery._read_backlog())  # raises nothing
    assert "backlog not read" in caplog.text


@pytest.mark.parametrize(
    "error, code",
    [
        (NotFoundError("x"), "RESOURCE_NOT_FOUND"),
        (PostgresUnavailableError("x"), "POSTGRES_UNAVAILABLE"),
        (KeyError("x"), "INTERNAL_ERROR"),
    ],
)
def test_an_mcp_failure_counts_under_the_code_it_answers(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    """As HTTP labels it: a refusal's, a named 5xx's, or INTERNAL_ERROR."""
    from server.observability import otel
    from server.transport import mcp_outcome

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(otel, "record_op", lambda *a, **k: seen.append(k))
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)

    class _Op:
        name = "memory.delete"
        idempotent = True

    mcp_outcome._handler_failed(
        error, 9, "loom_memory_delete", _Op(), {}, None, None, "rid", None, rl_ns="n", elapsed_ms=3
    )
    (call,) = [k for k in seen if k.get("success") is False]
    assert call["transport"] == "mcp" and call["error_code"] == code


@pytest.mark.parametrize("counted, reported, capped", [(3, 3, False), (4, 4, False), (5, 4, True)])
def test_a_backlog_is_capped_only_past_the_cap(counted: int, reported: int, capped: bool) -> None:
    """The reads count up to `cap + 1`: exactly the cap is a full count, one more is not."""
    row = {"pending": counted, "parked": 0, "oldest": 1.5}
    b = pg_pipeline.backlog_from(row, 4)
    assert (b.pending, b.capped, b.oldest_age_s) == (reported, capped, 1.5)
