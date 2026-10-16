"""The `acted` derivation of the usage fold.

One join statement per pass over memory_exposures ⋈ memory_versions(run_id);
rows become `acted` usage events through the key→id funnel; a lag window is
re-read and the pairs credited inside it remembered,
so nothing is credited twice; the fold itself is untouched by a failure.
Stub ClickHouse, no network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory.workers import usage_fold
from server.memory.workers.usage_fold import (
    ACTED_CREDITS_SQL,
    ACTED_CREDITS_TABLE,
    ACTED_STATEMENT,
    UsageFoldResult,
    acted_pass,
    run_pass,
)
from server.workers.status import get_registry


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []
        # The committed credit marks, answered to the credits read.
        self.credits: list[dict[str, Any]] = []
        self.credit_reads: list[dict[str, Any]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        if sql == ACTED_CREDITS_SQL:
            self.credit_reads.append(params or {})
            return [
                c for c in self.credits if c["first_write"] > params["since"].replace(tzinfo=None)
            ]
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        if table == ACTED_CREDITS_TABLE:
            assert settings == {"async_insert": 0, "wait_for_async_insert": 1}
            self.credits.extend(rows)
            return
        self.inserted.append((table, rows))

    async def execute(self, sql, params=None):
        pass


T0 = datetime(2026, 9, 14, 22, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):

    usage_fold.reset_watermark()
    get_registry().reset()
    yield
    usage_fold.reset_watermark()
    get_registry().reset()


@pytest.fixture
def events(monkeypatch) -> list[dict[str, Any]]:
    """Capture the usage events the key→id funnel would buffer; the funnel's
    own id lookup answers from the stub's query_all."""
    from server.memory.store import usage as u

    out: list[dict[str, Any]] = []

    async def record(**kw):
        out.append(kw)

    monkeypatch.setattr(u, "record_usage_event", record)
    return out


def _acted_row(
    key: str, run: str = "r1", how: str = "revised", ns: str = "n", agent: str = "cursor"
):
    return {
        "org": "o",
        "namespace": ns,
        "run_id": run,
        "acted_key": key,
        "how": how,
        "agent": agent,
        "first_write": T0.replace(tzinfo=None),
    }


def _id_rows(*keys: str, ns: str = "n"):
    return [{"memory_id": f"id-{k}", "namespace": ns, "memory_key": k} for k in keys]


def test_statement_shape_is_the_one_join_the_record_names() -> None:
    s = ACTED_STATEMENT
    assert "loom.memory_exposures" in s and "ARRAY JOIN returned_keys" in s
    assert "loom.memory_versions" in s and "run_id NOT IN ('-', '')" in s
    for how in ("'revised'", "'deleted'", "'duplicate_of'", "'superseded'"):
        assert how in s
    # PREWHERE is banned; parameters are typed.
    assert "PREWHERE" not in s
    for p in ("{since:DateTime}", "{since_exp:DateTime}", "{lim:UInt32}"):
        assert p in s
    # A same-key rewrite counts only after the key was shown to the run.
    assert "w.created_at > expk.first_seen" in s


def test_cold_start_watermark_is_the_last_acted_event_or_a_day_back(events) -> None:
    ch = StubCH()
    ch.query_one_returns.append({"last": None})

    async def _run() -> None:
        await acted_pass(ch, now=T0)
        assert usage_fold.current_acted_watermark() == T0 - timedelta(
            seconds=usage_fold.ACTED_WRITE_LAG_SECONDS
        )
        sql, params = ch.query_all_calls[0]
        assert sql == ACTED_STATEMENT
        assert params["since"] == T0 - timedelta(days=1)
        assert params["since_exp"] == params["since"] - timedelta(
            seconds=usage_fold.ACTED_EXPOSURE_LOOKBACK_SECONDS
        )
        assert params["lim"] == usage_fold.ACTED_MAX_ROWS_PER_PASS
        assert "event_type = 'acted'" in ch.query_one_calls[0][0]

    asyncio.run(_run())
    usage_fold.reset_watermark()
    ch2 = StubCH()
    last = (T0 - timedelta(hours=3)).replace(tzinfo=None)
    ch2.query_one_returns.append({"last": last})
    asyncio.run(acted_pass(ch2, now=T0))
    assert ch2.query_all_calls[0][1]["since"] == T0 - timedelta(hours=3) - timedelta(
        seconds=usage_fold.ACTED_WRITE_LAG_SECONDS
    )


def test_rows_become_acted_events_once_per_org_agent_through_the_key_funnel(events) -> None:
    ch = StubCH()
    ch.query_one_returns.append(None)
    ch.query_all_returns.append(
        [
            _acted_row("k1"),
            _acted_row("k2", how="duplicate_of"),
            _acted_row("k3", run="r2", agent="claude-code"),
            _acted_row("k1", run="r2", agent="claude-code"),  # same key, another run
        ]
    )
    ch.query_all_returns.append(_id_rows("k1", "k2"))  # cursor's lookup
    ch.query_all_returns.append(_id_rows("k3", "k1"))  # claude-code's lookup
    res = asyncio.run(acted_pass(ch, now=T0))
    assert res.acted_candidates == 4 and res.acted_emitted == 4 and res.acted_ms >= 0
    # One funnel call per (org, agent), each with its namespace→keys map.
    lookups = [p for s, p in ch.query_all_calls[1:]]
    assert sorted(sorted(p["keys"]) for p in lookups) == [["k1", "k2"], ["k1", "k3"]]
    assert all(p["org"] == "o" and p["namespaces"] == ["n"] for p in lookups)
    assert {e["event_type"] for e in events} == {"acted"}
    assert sorted((e["memory_id"], e["agent"]) for e in events) == [
        ("id-k1", "claude-code"),
        ("id-k1", "cursor"),
        ("id-k2", "cursor"),
        ("id-k3", "claude-code"),
    ]
    assert all(e["org"] == "o" and e["namespace"] == "n" and e["event_count"] == 1 for e in events)


def test_the_lag_window_is_reread_but_pairs_are_credited_once(events) -> None:
    ch = StubCH()
    ch.query_one_returns.append(None)
    ch.query_all_returns.append([_acted_row("k1")])
    ch.query_all_returns.append(_id_rows("k1"))
    asyncio.run(acted_pass(ch, now=T0))
    wm1 = usage_fold.current_acted_watermark()
    assert wm1 == T0 - timedelta(seconds=usage_fold.ACTED_WRITE_LAG_SECONDS)
    # Next pass, a minute later: the same pair is inside the re-read window
    # and comes back; a new pair appears beside it.
    later = T0 + timedelta(seconds=60)
    ch.query_all_returns.append([_acted_row("k1"), _acted_row("k9")])
    ch.query_all_returns.append(_id_rows("k9"))
    res = asyncio.run(acted_pass(ch, now=later))
    assert ch.query_all_calls[2][1]["since"] == wm1  # the watermark, not now
    assert res.acted_candidates == 2 and res.acted_emitted == 1
    assert [e["memory_id"] for e in events] == ["id-k1", "id-k9"]
    assert sorted(ch.query_all_calls[3][1]["keys"]) == ["k9"]
    assert usage_fold.current_acted_watermark() == later - timedelta(
        seconds=usage_fold.ACTED_WRITE_LAG_SECONDS
    )
    # Nothing new: no funnel call, watermark still advances with the clock.
    ch.query_all_returns.append([])
    asyncio.run(acted_pass(ch, now=later + timedelta(seconds=60)))
    assert len(ch.query_all_calls) == 5
    assert usage_fold.current_acted_watermark() == later + timedelta(seconds=60) - timedelta(
        seconds=usage_fold.ACTED_WRITE_LAG_SECONDS
    )


def test_a_new_leader_rereading_the_window_credits_nothing_twice(events) -> None:
    """The credited pairs live in ClickHouse, not the leader's memory: a cold
    start (another replica taking the lease) re-reads the lag window and
    emits nothing for the pairs the old leader already credited."""
    ch = StubCH()
    ch.query_one_returns.append(None)
    ch.query_all_returns.append([_acted_row("k1")])
    ch.query_all_returns.append(_id_rows("k1"))
    asyncio.run(acted_pass(ch, now=T0))
    assert [c["pair_sha256"] for c in ch.credits] == [
        usage_fold._pair_sha256(("o", "n", "r1", "k1"))
    ]
    assert set(ch.credits[0]) == {"pair_sha256", "first_write"}
    # New leader: process state gone, the table is not.
    usage_fold.reset_watermark()
    ch.query_one_returns.append({"last": (T0 + timedelta(seconds=5)).replace(tzinfo=None)})
    ch.query_all_returns.append([_acted_row("k1"), _acted_row("k9")])
    ch.query_all_returns.append(_id_rows("k9"))
    res = asyncio.run(acted_pass(ch, now=T0 + timedelta(seconds=30)))
    assert res.acted_emitted == 1
    assert [e["memory_id"] for e in events] == ["id-k1", "id-k9"]
    assert len(ch.credits) == 2


def test_a_new_leader_on_a_lagging_replica_credits_nothing_twice(events) -> None:
    """The old leader's marks are committed but not yet on the replica the new
    leader reads from: only a sequentially consistent read sees them."""
    from server.core.ch import sequential_reads_active

    class Lagging(StubCH):
        async def query_all(self, sql, params=None):
            if sql == ACTED_CREDITS_SQL and not sequential_reads_active():
                return []
            return await super().query_all(sql, params)

    ch = Lagging()
    ch.query_one_returns.append(None)
    ch.query_all_returns.append([_acted_row("k1")])
    ch.query_all_returns.append(_id_rows("k1"))
    asyncio.run(acted_pass(ch, now=T0))
    usage_fold.reset_watermark()  # the lease moves: the new leader's memory is empty
    ch.query_one_returns.append({"last": (T0 + timedelta(seconds=5)).replace(tzinfo=None)})
    ch.query_all_returns.append([_acted_row("k1")])
    ch.query_all_returns.append(_id_rows("k1"))
    res = asyncio.run(acted_pass(ch, now=T0 + timedelta(seconds=30)))
    assert res.acted_emitted == 0
    assert [e["memory_id"] for e in events] == ["id-k1"]


def test_a_schema_without_the_credits_table_still_credits(events) -> None:
    class NoTable(StubCH):
        async def query_all(self, sql, params=None):
            if sql == ACTED_CREDITS_SQL:
                raise RuntimeError("Code: 60. Unknown table")
            return await super().query_all(sql, params)

        async def insert(self, table, rows, settings=None):
            if table == ACTED_CREDITS_TABLE:
                raise RuntimeError("Code: 60. Unknown table")
            await super().insert(table, rows, settings)

    ch = NoTable()
    ch.query_one_returns.append(None)
    ch.query_all_returns.append([_acted_row("k1")])
    ch.query_all_returns.append(_id_rows("k1"))
    res = asyncio.run(acted_pass(ch, now=T0))
    assert res.acted_emitted == 1
    # ...and the in-process set still dedups the re-read window.
    ch.query_all_returns.append([_acted_row("k1")])
    res = asyncio.run(acted_pass(ch, now=T0 + timedelta(seconds=60)))
    assert len(events) == 1


def test_a_full_page_holds_the_watermark_at_the_last_credited_write(events, monkeypatch) -> None:
    monkeypatch.setattr(usage_fold, "ACTED_MAX_ROWS_PER_PASS", 2)
    ch = StubCH()
    ch.query_one_returns.append(None)
    first = (T0 - timedelta(hours=5)).replace(tzinfo=None)
    second = (T0 - timedelta(hours=4)).replace(tzinfo=None)
    rows = [_acted_row("k1"), _acted_row("k2")]
    rows[0]["first_write"] = first
    rows[1]["first_write"] = second
    ch.query_all_returns.append(rows)
    ch.query_all_returns.append(_id_rows("k1", "k2"))
    asyncio.run(acted_pass(ch, now=T0))
    assert usage_fold.current_acted_watermark() == T0 - timedelta(hours=4) - timedelta(
        seconds=usage_fold.ACTED_WRITE_LAG_SECONDS
    )


def test_run_pass_runs_acted_first_and_a_failure_never_stops_the_fold(events, monkeypatch) -> None:
    ch = StubCH()
    ch.query_one_returns.append(None)  # acted watermark derivation
    ch.query_all_returns.append([_acted_row("k1")])
    ch.query_all_returns.append(_id_rows("k1"))
    ch.query_one_returns.append({"hwm": None})  # fold probe: nothing to fold
    res = asyncio.run(run_pass(ch=ch))
    assert res.acted_candidates == 1 and res.acted_emitted == 1
    rec = get_registry().snapshot()["usage_fold"]
    assert rec["last_outcome"]["acted_emitted"] == 1 and rec["last_outcome"]["candidates"] == 0

    # A broken join is logged and the fold still runs.
    usage_fold.reset_watermark()
    get_registry().reset()

    class Boom(StubCH):
        async def query_all(self, sql, params=None):
            if sql == ACTED_STATEMENT:
                raise RuntimeError("Code: 207. ambiguous identifier")
            return await super().query_all(sql, params)

    ch2 = Boom()
    ch2.query_one_returns.append(None)
    ch2.query_one_returns.append({"hwm": None})
    from server.workers import pass_context

    ctx = pass_context.begin("usage_fold")
    try:
        res2 = asyncio.run(run_pass(ch=ch2))
    finally:
        pass_context.clear()
    assert res2.acted_candidates == 0 and res2.acted_emitted == 0
    assert get_registry().snapshot()["usage_fold"]["last_outcome"]["candidates"] == 0
    # ...and the pass is partial, not a clean one.
    assert ctx.count == 1 and ctx.failures[0][:2] == ("acted", "RuntimeError")


def test_result_dataclass_carries_the_acted_counters() -> None:
    r = UsageFoldResult()
    assert (r.acted_candidates, r.acted_emitted, r.acted_ms) == (0, 0, 0)
