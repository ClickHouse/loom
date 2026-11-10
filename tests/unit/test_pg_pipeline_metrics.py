"""The Postgres publication pipeline's metrics (`server.observability.pg_pipeline`)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import pg_pipeline
from server.settings import settings


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
    now = [1000.0]
    monkeypatch.setattr(pg_pipeline.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(settings, "otel_metrics_export_interval_s", 60)
    pg_pipeline.set_backlog("memory", pg_pipeline.Backlog(3, 0, 1.5))
    assert pg_pipeline.fresh_backlog()["memory"].pending == 3
    now[0] += 121
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
    [({}, False, None), ({"n": 5}, False, "passed"), ({"n": 5}, True, "behind_503")],
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


def test_health_reads_the_backlog_itself_off_the_lease_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import health
    from server.knowledge.store import pg_documents
    from server.memory.store import pg_memory

    reads: list[str] = []

    def reading(source: str) -> Any:
        async def read(cap: int) -> pg_pipeline.Backlog:
            reads.append(source)
            return pg_pipeline.Backlog(1, 0, 2.0)

        return read

    monkeypatch.setattr(pg_pipeline, "fresh_backlog", lambda: {})
    monkeypatch.setattr(pg_memory, "backlog", reading("memory"))
    monkeypatch.setattr(pg_memory, "outbox_backlog", reading("audit_outbox"))
    monkeypatch.setattr(pg_documents, "backlog", reading("knowledge"))
    monkeypatch.setattr(health, "_backlog_cache", {"at": -100.0, "value": None})
    first = asyncio.run(health._backlog())
    asyncio.run(health._backlog())  # within the TTL: no second reading
    assert first["memory"] == {"pending": 1, "parked": 0, "oldest_age_s": 2.0, "capped": False}
    assert reads == ["memory", "audit_outbox", "knowledge"]
