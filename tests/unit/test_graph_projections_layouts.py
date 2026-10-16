"""loom.graph_projections, both layouts, against a stub client: what each statement
says and how its answer is read. The live behaviour (strict inserts, version-checked
raises, the epoch of an empty maxIf) was checked against ClickHouse 26.5."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory.store import graph_projections as gp

NOW = datetime(2026, 10, 14, 12, 0, 0)


class _Keeper:
    """A KeeperMap table: key -> (started_at, finished); strict mode semantics."""

    def __init__(self) -> None:
        self.keys: dict[str, tuple[datetime, int]] = {}
        self.sql: list[str] = []
        self.delay_s = 0.0
        self.reads: list[list[str]] = []
        self.lose_raises = 0
        self.fail_insert: Exception | None = None

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if "system.tables" in sql:
            return {"engine": "KeeperMap"}
        assert "token IN {tokens:Array(String)}" in sql  # by key, never a scan
        tokens = (params or {})["tokens"]
        self.reads.append(tokens)
        rows = [(k, *self.keys[k]) for k in tokens if k in self.keys]
        return {"now": NOW, "keys": rows}

    async def execute(self, sql: str, params: dict[str, Any], **_: Any) -> None:
        self.sql.append(sql.split()[0])
        await asyncio.sleep(self.delay_s)
        key = params["token"]
        if sql.startswith("INSERT"):
            assert "keeper_map_strict_mode = 1" in sql
            if self.fail_insert is not None:
                raise self.fail_insert
            if key in self.keys:
                raise RuntimeError("Code: 999. Transaction failed (Node exists)")
            self.keys[key] = (NOW, params["finished"])
        elif "UPDATE" in sql:
            if self.lose_raises:
                self.lose_raises -= 1
                raise RuntimeError("Code: 999. Bad version (KEEPER_EXCEPTION)")
            self.keys[key] = (NOW + timedelta(seconds=1), 1)
        elif "started_at <" in sql:
            if (NOW - self.keys[key][0]).total_seconds() * 1000 > params["ms"]:
                del self.keys[key]
        else:
            self.keys.pop(key, None)


@pytest.fixture(autouse=True)
def _fresh():
    gp._reset_for_tests()
    yield
    gp._reset_for_tests()


@pytest.mark.asyncio
async def test_a_keeper_begin_claims_one_slot_of_its_namespace() -> None:
    ch = _Keeper()
    token = await gp.begin("o", "n", stale_s=15, ch=ch)
    assert token is not None and token.startswith("o\x1fn\x1f")
    assert ch.reads == []  # one insert, no read


@pytest.mark.asyncio
async def test_a_held_slot_sends_the_begin_to_a_free_one_and_clears_a_dead_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _Keeper()
    monkeypatch.setattr(gp, "SLOTS", 2)
    monkeypatch.setattr(gp.random, "randrange", lambda _n: 0)
    ch.keys["o\x1fn\x1f0"] = (NOW, 0)
    ch.keys["o\x1fn\x1f1"] = (NOW - timedelta(seconds=30), 0)  # its process died
    assert await gp.begin("o", "n", stale_s=15, ch=ch) == "o\x1fn\x1f1"
    assert ch.reads == [["o\x1fn\x1f0", "o\x1fn\x1f1", "o\x1fn\x1fw"]]
    ch.keys["o\x1fn\x1f1"] = (NOW, 0)
    assert await gp.begin("o", "n", stale_s=15, ch=ch) is None  # every slot held


@pytest.mark.asyncio
async def test_a_failed_keeper_insert_is_uncertain_and_names_its_key() -> None:
    ch = _Keeper()
    ch.fail_insert = TimeoutError("timed out")
    with pytest.raises(gp.BeginUncertain) as e:
        await gp.begin("o", "n", stale_s=15, ch=ch)
    assert e.value.token.startswith("o\x1fn\x1f")


@pytest.mark.asyncio
async def test_a_write_is_inserted_once_then_raised_and_a_lost_race_is_retried() -> None:
    ch = _Keeper()
    await gp.note_writes([("o", "n")], ch=ch)
    assert ch.sql == ["INSERT"]
    ch.lose_raises = 2
    await gp.note_writes([("o", "n")], ch=ch)
    assert ch.sql == ["INSERT", "ALTER", "ALTER", "ALTER"]  # the key exists: no INSERT
    assert ch.keys["o\x1fn\x1fw"][1] == 1


@pytest.mark.asyncio
async def test_concurrent_raises_of_a_namespace_share_one_in_flight_and_one_queued() -> None:
    ch = _Keeper()
    await gp.note_writes([("o", "n")], ch=ch)
    ch.delay_s = 0.05
    first = asyncio.ensure_future(gp.note_writes([("o", "n")], ch=ch))
    await asyncio.sleep(0.01)  # in flight: the 48 below come after it began
    await asyncio.gather(first, *(gp.note_writes([("o", "n")], ch=ch) for _ in range(48)))
    assert ch.sql == ["INSERT", "ALTER", "ALTER"]


@pytest.mark.asyncio
async def test_an_end_frees_its_slot_unless_held_past_the_stale_bound() -> None:
    ch = _Keeper()
    token = await gp.begin("o", "n", stale_s=15, ch=ch)
    assert token is not None
    await gp.end("o", "n", token, held_s=14.5, stale_s=15, ch=ch)
    assert token in ch.keys  # another projection may hold it by now
    await gp.end("o", "n", token, held_s=0.2, stale_s=15, ch=ch)
    assert token not in ch.keys


@pytest.mark.asyncio
async def test_an_uncertain_begins_end_leaves_the_slot_to_go_stale() -> None:
    """Its insert may never have landed, and another projection may hold the slot."""
    ch = _Keeper()
    ch.keys["o\x1fn\x1f3"] = (NOW, 0)  # another projection's
    await gp.end("o", "n", "o\x1fn\x1f3", held_s=0.2, stale_s=15, certain=False, ch=ch)
    assert "o\x1fn\x1f3" in ch.keys and ch.sql == []


@pytest.mark.asyncio
async def test_a_keeper_snapshot_reads_the_slots_and_the_write() -> None:
    ch = _Keeper()
    ch.keys["o\x1fn\x1f3"] = (NOW - timedelta(seconds=1), 0)
    ch.keys["o\x1fn\x1f4"] = (NOW - timedelta(seconds=30), 0)  # dead
    ch.keys["o\x1fn\x1fw"] = (NOW - timedelta(seconds=2), 1)
    snap = await gp.snapshot("o", "n", stale_s=15, ch=ch)
    assert snap.now == NOW.replace(tzinfo=UTC)
    assert len(snap.busy) == 1 and next(iter(snap.busy)).startswith("o\x1fn\x1f3@")
    assert snap.written == (NOW - timedelta(seconds=2)).replace(tzinfo=UTC)
    assert len(ch.reads[0]) == gp.SLOTS + 1
