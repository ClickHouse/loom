"""The code plane's process cache: an entry lives its TTL, a put may name a shorter
one, the least recently used entry goes past the bound, and `clear_all` empties every
cache."""

from __future__ import annotations

import time

import pytest

from server.code.app.ttl_cache import TtlCache, clear_all


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def test_an_entry_lives_its_ttl(clock: list[float]) -> None:
    c: TtlCache[str, int] = TtlCache(10.0)
    c.put("a", 1)
    clock[0] += 9.9
    assert c.get("a") == 1 and "a" in c
    clock[0] += 0.2
    assert c.get("a") is None and "a" not in c and len(c) == 0


def test_a_put_may_name_its_own_ttl(clock: list[float]) -> None:
    c: TtlCache[str, bool] = TtlCache(3600.0)
    c.put("failed", False, ttl_s=30.0)
    c.put("ok", True)
    clock[0] += 31
    assert c.get("failed") is None and c.get("ok") is True


def test_a_stored_none_is_told_apart_with_a_default(clock: list[float]) -> None:
    c: TtlCache[str, int | None] = TtlCache(10.0)
    miss = object()
    c.put("k", None)
    assert c.get("k", miss) is None and c.get("absent", miss) is miss


def test_the_least_recently_used_entry_goes_past_the_bound(clock: list[float]) -> None:
    c: TtlCache[str, int] = TtlCache(10.0, maxsize=2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # a is now the most recent
    c.put("c", 3)
    assert (c.get("a"), c.get("b"), c.get("c")) == (1, None, 3)


def test_clear_all_empties_every_cache(clock: list[float]) -> None:
    one: TtlCache[str, int] = TtlCache(10.0)
    two: TtlCache[str, int] = TtlCache(10.0)
    one.put("a", 1)
    two.put("b", 2)
    clear_all()
    assert len(one) == len(two) == 0


def test_a_callable_ttl_is_read_at_each_put(clock: list[float]) -> None:
    """A cache whose TTL is a setting reads it per put: a change applies to the next entry."""
    ttl = [10.0]
    c: TtlCache[str, int] = TtlCache(lambda: ttl[0])
    c.put("a", 1)
    ttl[0] = 2.0
    c.put("b", 2)
    assert c.ttl_s == 2.0
    clock[0] += 3.0
    assert c.get("a") == 1 and c.get("b") is None
