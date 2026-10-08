"""A restart does not restart a long interval.

A worker whose last pass in this deployment family is older than its interval
starts within OVERDUE_SPREAD_SECONDS of boot instead of after its crc32
stagger; an unknown last pass keeps the stagger (fail-quiet)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.workers import status_store, supervisor


def _spec(name: str = "code_backport_gaps", interval: float = 86400.0) -> SimpleNamespace:
    return SimpleNamespace(name=name, interval_seconds=interval, first_pass_hook=lambda: None)


def test_label_family_spans_the_colours_and_nothing_else() -> None:
    assert status_store.label_family("deploy:workers:green") == "deploy:workers:"
    assert status_store.label_family("deploy:workers:blue") == "deploy:workers:"
    assert status_store.label_family("iso:bench-7") == "iso:bench-7"
    assert status_store.label_family("") == ""


@pytest.mark.asyncio
async def test_an_overdue_worker_starts_within_the_spread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    async def last(worker: str, family: str, **_: object) -> datetime:
        seen["family"] = family
        return datetime.now(UTC) - timedelta(hours=50)

    monkeypatch.setattr(status_store, "last_pass_finished", last)
    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "deploy:workers:green")
    stagger = supervisor.first_pass_delay(_spec())
    assert stagger > supervisor.OVERDUE_SPREAD_SECONDS  # a 15 h stagger
    delay = await supervisor._overdue_delay(_spec(), stagger)
    assert delay is not None and 0 <= delay < supervisor.OVERDUE_SPREAD_SECONDS
    assert seen["family"] == "deploy:workers:"


@pytest.mark.asyncio
async def test_a_recent_pass_keeps_the_stagger(monkeypatch: pytest.MonkeyPatch) -> None:
    async def last(worker: str, family: str, **_: object) -> datetime:
        return datetime.now(UTC) - timedelta(hours=2)

    monkeypatch.setattr(status_store, "last_pass_finished", last)
    assert await supervisor._overdue_delay(_spec(), 50_000.0) is None


@pytest.mark.asyncio
async def test_an_unknown_last_pass_keeps_the_stagger(monkeypatch: pytest.MonkeyPatch) -> None:
    async def last(worker: str, family: str, **_: object) -> None:
        return None

    monkeypatch.setattr(status_store, "last_pass_finished", last)
    assert await supervisor._overdue_delay(_spec(), 50_000.0) is None


@pytest.mark.asyncio
async def test_begin_loop_marks_the_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    async def last(worker: str, family: str, **_: object) -> datetime:
        return datetime.now(UTC) - timedelta(days=3)

    async def not_due(spec: object) -> bool:
        return False

    async def no_cursor(worker: str, **_: object) -> str:
        return ""

    monkeypatch.setattr(status_store, "last_pass_finished", last)
    monkeypatch.setattr(status_store, "resume_cursor", no_cursor)
    monkeypatch.setattr(supervisor, "first_pass_is_due", not_due)
    delay, state = await supervisor._begin_loop(_spec())  # type: ignore[arg-type]
    assert state.trigger == "overdue" and delay < supervisor.OVERDUE_SPREAD_SECONDS
    # ...and the row keeps it: a trigger outside the store's vocabulary is written as ''.
    now = datetime.now(UTC)
    row = status_store.build_row("w", status="ok", started=now, finished=now, trigger=state.trigger)
    assert row["trigger"] == "overdue"


@pytest.mark.asyncio
async def test_the_store_read_is_fail_quiet() -> None:
    class _Broken:
        async def query_one(self, *_: object, **__: object) -> None:
            raise RuntimeError("no clickhouse")

    assert await status_store.last_pass_finished("w", "deploy:workers:", ch=_Broken()) is None
