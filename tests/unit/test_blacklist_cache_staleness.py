"""The blacklist cache re-reads its version watermark at most once per check
interval, not on every write once the first interval has passed, and still
refreshes when the version moves or the TTL runs out. CH-free."""

from __future__ import annotations

from datetime import datetime

import pytest

import server.memory.app.blacklist_guard as bl


class _CH:
    def __init__(self) -> None:
        self.version = 1
        self.version_reads = 0
        self.row_reads = 0

    async def query_one(self, sql, params=None):
        self.version_reads += 1
        return {"n": 1, "s": self.version}

    async def query_all(self, sql, params=None):
        self.row_reads += 1
        return []


@pytest.fixture
def env(monkeypatch):
    ch = _CH()
    clock = {"t": 1000.0}
    monkeypatch.setattr(bl, "get_client", lambda: ch)
    monkeypatch.setattr(bl.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(bl.settings, "blacklist_version_check_interval_seconds", 2.0)
    monkeypatch.setattr(bl.settings, "blacklist_cache_ttl_seconds", 300)
    monkeypatch.setattr(bl, "_dirty", True)
    monkeypatch.setattr(bl, "_cache", [])
    return ch, clock


async def _writes(n: int, clock: dict[str, float], step: float) -> None:
    for _ in range(n):
        clock["t"] += step
        await bl.find_match(org="o", namespace="ns", value="v")


async def test_an_unchanged_version_is_read_once_per_interval(env) -> None:
    ch, clock = env
    await bl.find_match(org="o", namespace="ns", value="v")  # first load
    assert ch.row_reads == 1
    reads_after_load = ch.version_reads
    # 100 writes, 0.1 s apart: 10 s of traffic, a 2 s interval -> 5 checks.
    await _writes(100, clock, 0.1)
    assert ch.version_reads - reads_after_load == 5
    assert ch.row_reads == 1


async def test_a_moved_version_refreshes_the_rows(env) -> None:
    ch, clock = env
    await bl.find_match(org="o", namespace="ns", value="v")
    ch.version = 2
    await _writes(1, clock, 2.5)
    assert ch.row_reads == 2


async def test_the_ttl_refreshes_even_when_the_version_holds(env) -> None:
    ch, clock = env
    await bl.find_match(org="o", namespace="ns", value="v")
    await _writes(1, clock, 301.0)
    assert ch.row_reads == 2


async def test_refusals_are_coalesced_into_one_write_per_pattern_per_interval(env) -> None:
    ch, clock = env
    executed: list[dict] = []

    async def _exec(sql, params=None):
        executed.append(dict(params or {}))

    ch.execute = _exec
    bl._pending_hits.clear()
    bl._last_hit_flush.clear()
    for _ in range(100):  # a client retrying a refused write
        clock["t"] += 0.1
        await bl.note_hit("00000000-0000-0000-0000-000000000001")
    # 10 s of refusals, a 30 s interval: the first one writes, the rest wait
    assert [{k: p[k] for k in ("pid", "n")} for p in executed] == [
        {"pid": "00000000-0000-0000-0000-000000000001", "n": 1}
    ]
    assert isinstance(executed[0]["at"], datetime)  # bound: a replicated table refuses now()
    clock["t"] += 30.0
    await bl.note_hit("00000000-0000-0000-0000-000000000001")
    assert executed[-1]["n"] == 100  # every refusal counted, in one mutation
    await bl.note_hit("00000000-0000-0000-0000-000000000001")
    assert await bl.flush_hits() == 1 and executed[-1]["n"] == 1
    assert await bl.flush_hits() == 0


async def test_a_refresh_that_read_earlier_never_replaces_one_that_read_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two refreshes overlap: the one that started first and finishes last leaves the
    later one's cache in place."""
    import asyncio

    reads = iter([(1, 1), (2, 2)])
    slow = asyncio.Event()

    async def _version() -> tuple[int, int]:
        return next(reads)

    async def _rows(*, ch: object) -> list[dict[str, object]]:
        if not slow.is_set():
            slow.set()
            await asyncio.sleep(0.05)  # the first read is held back past the second's
        return []

    monkeypatch.setattr(bl, "_current_version", _version)
    monkeypatch.setattr(bl._repo, "active_patterns", _rows)
    monkeypatch.setattr(bl, "get_client", lambda: object())
    for name, value in (("_installed", -1), ("_invalidated", -1), ("_cache", []), ("_dirty", True)):
        monkeypatch.setattr(bl, name, value)
    # The refresh overwrites these: kept as they are, so they are restored after the test.
    for name in ("_cached_version", "_last_version_check", "_loaded_at"):
        monkeypatch.setattr(bl, name, getattr(bl, name))
    await asyncio.gather(bl._refresh_cache(), bl._refresh_cache())
    assert bl._cached_version == (2, 2)


async def test_a_refresh_that_started_before_an_invalidation_leaves_the_cache_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pattern added while a refresh reads: that read predates it, so the cache it
    installs (a cold one keeps the patterns it read) stays dirty, and the next check
    reads again."""
    import asyncio

    reading = asyncio.Event()
    row = {
        "pattern_id": "p1",
        "org": "o",
        "namespace": "ns",
        "pattern": "secret",
        "pattern_type": "substring",
        "scope": "org",
        "reason": "",
    }

    async def _version() -> tuple[int, int]:
        return (1, 1)

    async def _rows(*, ch: object) -> list[dict[str, object]]:
        reading.set()
        await asyncio.sleep(0.05)
        return [row]

    monkeypatch.setattr(bl, "_current_version", _version)
    monkeypatch.setattr(bl._repo, "active_patterns", _rows)
    monkeypatch.setattr(bl, "get_client", lambda: object())
    for name, value in (("_installed", -1), ("_invalidated", -1), ("_cache", []), ("_dirty", True)):
        monkeypatch.setattr(bl, name, value)
    # The refresh overwrites these: kept as they are, so they are restored after the test.
    for name in ("_cached_version", "_last_version_check", "_loaded_at"):
        monkeypatch.setattr(bl, name, getattr(bl, name))

    async def _add_while_reading() -> None:
        await reading.wait()
        bl.invalidate_cache()

    await asyncio.gather(bl._refresh_cache(), _add_while_reading())
    assert [p.pattern_id for p in bl._cache] == ["p1"]
    assert bl._dirty
