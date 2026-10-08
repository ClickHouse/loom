"""A status reads what the pass published.

Counting `loom.code_symbol_stats` and `loom.code_unit_risk` under FINAL on
every `code.index_status` open past a ten-minute memo costs tens of MB per
open for two fields one weekly worker writes. The graph-stats pass publishes
`symbol_stats_summary` / `unit_risk_summary` state rows after its legs;
the handler reads the row and counts (and publishes) only when there is
none.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.code.app import index_status
from server.code.store import index as cr


@pytest.fixture(autouse=True)
def _fresh() -> None:
    index_status._SYMBOL_STATS_CACHE.clear()
    index_status._UNIT_RISK_CACHE.clear()


def _row(n: int, at: str) -> dict[str, Any]:
    return {"value": json.dumps({"n": n, "at": at}), "detail": "", "updated_at": None}


@pytest.mark.asyncio
async def test_a_published_row_is_the_answer_and_nothing_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(self: Any, org: str, ns: str) -> tuple[int, datetime | None]:
        raise AssertionError("a published row must not be recounted")

    monkeypatch.setattr(cr.CodeRepo, "symbol_stats_summary", never)
    monkeypatch.setattr(cr.CodeRepo, "unit_risk_summary", never)
    got = await index_status.symbol_stats_summary_cached(
        "o", "n", published=_row(98785, "2026-09-16T10:29:48+00:00")
    )
    assert got == (98785, "2026-09-16T10:29:48+00:00")
    got2 = await index_status.unit_risk_summary_cached("o", "n", published=_row(0, ""))
    assert got2 == (0, "")
    # remembered: the next open with no row in hand still answers from the memo
    assert await index_status.symbol_stats_summary_cached("o", "n") == (
        98785,
        "2026-09-16T10:29:48+00:00",
    )


@pytest.mark.asyncio
async def test_no_row_counts_once_and_publishes_for_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = 0
    published: list[tuple[str, str, str, int, str]] = []

    async def count(self: Any, org: str, ns: str) -> tuple[int, datetime | None]:
        nonlocal counts
        counts += 1
        return 7, datetime(2026, 9, 19, 8, 0, 0)

    async def publish(self: Any, org: str, ns: str, key: str, n: int, at: str) -> None:
        published.append((org, ns, key, n, at))

    monkeypatch.setattr(cr.CodeRepo, "symbol_stats_summary", count)
    monkeypatch.setattr(cr.CodeRepo, "publish_summary", publish)
    got = await index_status.symbol_stats_summary_cached("o", "n", published=None)
    assert got == (7, "2026-09-19T08:00:00+00:00") and counts == 1
    assert published == [("o", "n", "symbol_stats_summary", 7, "2026-09-19T08:00:00+00:00")]
    assert (
        await index_status.symbol_stats_summary_cached("o", "n", published=None) == got
        and counts == 1
    )


@pytest.mark.asyncio
async def test_a_caller_that_never_looked_counts_but_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def count(self: Any, org: str, ns: str) -> tuple[int, datetime | None]:
        return 3, None

    async def publish(self: Any, org: str, ns: str, key: str, n: int, at: str) -> None:
        raise AssertionError("only a caller that found no row publishes")

    monkeypatch.setattr(cr.CodeRepo, "unit_risk_summary", count)
    monkeypatch.setattr(cr.CodeRepo, "publish_summary", publish)
    assert await index_status.unit_risk_summary_cached("o", "n") == (3, "")


@pytest.mark.asyncio
async def test_a_garbage_row_is_not_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def count(self: Any, org: str, ns: str) -> tuple[int, datetime | None]:
        return 5, None

    async def publish(self: Any, org: str, ns: str, key: str, n: int, at: str) -> None:
        return None

    monkeypatch.setattr(cr.CodeRepo, "symbol_stats_summary", count)
    monkeypatch.setattr(cr.CodeRepo, "publish_summary", publish)
    bad = {"value": "{not json", "detail": ""}
    assert await index_status.symbol_stats_summary_cached("o", "n", published=bad) == (5, "")


def test_parse_published_summary_tolerates_garbage() -> None:
    assert cr.parse_published_summary(None) is None
    assert cr.parse_published_summary({"value": "{not json"}) is None
    assert cr.parse_published_summary({"value": json.dumps({"at": "x"})}) is None
    assert cr.parse_published_summary({"value": json.dumps({"n": "many"})}) is None
    assert cr.parse_published_summary({"value": json.dumps({"n": "12", "at": None})}) == (12, "")
    assert cr.summary_stamp(None) == ""
    assert cr.summary_stamp(datetime(2026, 9, 19, 8, 0, tzinfo=UTC)) == "2026-09-19T08:00:00+00:00"


@pytest.mark.asyncio
async def test_refresh_publishes_one_row_per_readable_table() -> None:
    class _Repo:
        def __init__(self) -> None:
            self.state: list[tuple[str, str, str]] = []

        async def symbol_stats_summary(self, org: str, ns: str) -> tuple[int, datetime | None]:
            return 11, datetime(2026, 9, 19, 8, 0, 0)

        async def unit_risk_summary(self, org: str, ns: str) -> tuple[int, datetime | None]:
            raise RuntimeError("table missing (behind a migration)")

        async def set_state(
            self, org: str, ns: str, key: str, value: str, detail: str = ""
        ) -> None:
            self.state.append((ns, key, value))

        refresh_stats_summaries = cr.CodeRepo.refresh_stats_summaries
        publish_summary = cr.CodeRepo.publish_summary

    repo = _Repo()
    out = await repo.refresh_stats_summaries("o", "n")
    assert out == {"symbol_stats_summary": (11, "2026-09-19T08:00:00+00:00")}
    ((ns, key, value),) = repo.state
    assert (ns, key) == ("n", "symbol_stats_summary")
    assert json.loads(value) == {"n": 11, "at": "2026-09-19T08:00:00+00:00"}


def test_the_pass_publishes_after_each_leg() -> None:
    from server.code.workers import graph_stats as w

    src = open(w.__file__, encoding="utf-8").read()

    def body(name: str) -> str:
        b = src[src.index(f"async def {name}(") :]
        ends = [i for i in (b.find("\nasync def ", 1), b.find("\ndef ", 1)) if i > 0]
        return b[: min(ends)] if ends else b

    repo_leg = body("_run_repo")
    assert repo_leg.index("_write_symbol_stats(") < repo_leg.index("_publish_summaries(")
    risk_leg = body("_run_unit_risk")
    assert risk_leg.index("_write_unit_risk(") < risk_leg.index("_publish_summaries(")


def test_the_status_handler_reads_the_state_before_the_summaries() -> None:
    src = open(index_status.__file__, encoding="utf-8").read()
    b = src[src.index("async def handle_code_index_status(") :]
    b = b[: b.index("\nasync def ", 1)]
    assert "_state_for_status(" in b
    assert 'published=state.get("symbol_stats_summary")' in b
    assert 'published=state.get("unit_risk_summary")' in b
