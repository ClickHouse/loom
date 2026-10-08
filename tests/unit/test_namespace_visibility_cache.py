"""The visibility lookup behind memory.search's private-first tier split.

Two properties matter:

* it must not put a ClickHouse round trip on every multi-namespace search, and
* when it cannot answer, the caller must end up searching *more* namespaces,
  never fewer — the failure direction is a relevance cost, not an access one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.identity.app import namespace_visibility as nv  # noqa: E402


class _FakeCH:
    """Counts queries so a cache hit is distinguishable from a repeat query."""

    def __init__(self, rows: list[dict] | None = None, boom: bool = False) -> None:
        self.rows = rows if rows is not None else []
        self.boom = boom
        self.calls = 0
        self.last_namespaces: list[str] = []

    async def query_all(self, sql: str, params: dict | None = None):
        self.calls += 1
        self.last_namespaces = list((params or {}).get("ns") or [])
        if self.boom:
            raise RuntimeError("clickhouse unreachable")
        wanted = set(self.last_namespaces)
        return [r for r in self.rows if r["namespace"] in wanted]


@pytest.fixture(autouse=True)
def _clean_cache():
    nv.reset()
    yield
    nv.reset()


@pytest.fixture
def patch_ch(monkeypatch):
    def _install(fake: _FakeCH) -> _FakeCH:
        monkeypatch.setattr("server.core.ch.get_client", lambda: fake)
        return fake

    return _install


@pytest.mark.asyncio
async def test_resolves_visibility(patch_ch) -> None:
    ch = patch_ch(
        _FakeCH(
            [
                {"namespace": "pub", "visibility": "public"},
                {"namespace": "priv", "visibility": "private"},
            ]
        )
    )
    got = await nv.visibility_map("org", ["pub", "priv"])
    assert got == {"pub": "public", "priv": "private"}
    assert ch.calls == 1


@pytest.mark.asyncio
async def test_second_call_is_served_from_cache(patch_ch) -> None:
    """The point of the module: one query, then none."""
    ch = patch_ch(_FakeCH([{"namespace": "pub", "visibility": "public"}]))
    for _ in range(5):
        assert await nv.visibility_map("org", ["pub"]) == {"pub": "public"}
    assert ch.calls == 1, "repeat searches must not re-query namespace visibility"


@pytest.mark.asyncio
async def test_only_uncached_namespaces_are_queried(patch_ch) -> None:
    """A partial hit queries the misses, not the whole set."""
    ch = patch_ch(
        _FakeCH(
            [
                {"namespace": "a", "visibility": "public"},
                {"namespace": "b", "visibility": "private"},
            ]
        )
    )
    await nv.visibility_map("org", ["a"])
    await nv.visibility_map("org", ["a", "b"])
    assert ch.calls == 2
    assert ch.last_namespaces == ["b"], "cached 'a' must not be re-queried"


@pytest.mark.asyncio
async def test_expiry_requeries(patch_ch, monkeypatch) -> None:
    ch = patch_ch(_FakeCH([{"namespace": "pub", "visibility": "public"}]))
    await nv.visibility_map("org", ["pub"])
    assert ch.calls == 1
    # Advance past the TTL without sleeping.
    real = nv.time.monotonic
    monkeypatch.setattr(nv.time, "monotonic", lambda: real() + nv._TTL_S + 1)
    await nv.visibility_map("org", ["pub"])
    assert ch.calls == 2


@pytest.mark.asyncio
async def test_query_failure_returns_empty_not_raise(patch_ch) -> None:
    """Fail open. An empty map routes the caller to the flat fan-out, which
    searches every namespace — more work, never less visibility."""
    ch = patch_ch(_FakeCH(boom=True))
    assert await nv.visibility_map("org", ["a", "b"]) == {}
    assert ch.calls == 1


@pytest.mark.asyncio
async def test_failure_is_not_cached(patch_ch) -> None:
    """A transient ClickHouse blip must not pin the flat path for a full TTL."""
    ch = patch_ch(_FakeCH(boom=True))
    await nv.visibility_map("org", ["a"])
    ch.boom = False
    ch.rows = [{"namespace": "a", "visibility": "public"}]
    assert await nv.visibility_map("org", ["a"]) == {"a": "public"}
    assert ch.calls == 2


@pytest.mark.asyncio
async def test_missing_row_is_not_invented(patch_ch) -> None:
    """A namespace with no row is absent from the map rather than guessed as
    private — the caller decides what unknown means."""
    patch_ch(_FakeCH([{"namespace": "a", "visibility": "public"}]))
    assert await nv.visibility_map("org", ["a", "nope"]) == {"a": "public"}


@pytest.mark.asyncio
async def test_orgs_do_not_share_entries(patch_ch) -> None:
    """Same namespace name under two orgs must not collide."""
    ch = patch_ch(
        _FakeCH(
            [
                {"namespace": "shared", "visibility": "public"},
            ]
        )
    )
    await nv.visibility_map("org-a", ["shared"])
    await nv.visibility_map("org-b", ["shared"])
    assert ch.calls == 2, "cache key must include org"


@pytest.mark.asyncio
async def test_overflow_drops_cache_rather_than_growing(patch_ch, monkeypatch) -> None:
    monkeypatch.setattr(nv, "_MAX_ENTRIES", 3)
    patch_ch(_FakeCH([{"namespace": f"n{i}", "visibility": "public"} for i in range(6)]))
    await nv.visibility_map("org", ["n0", "n1"])
    await nv.visibility_map("org", ["n2", "n3", "n4"])
    assert len(nv._cache) <= 3
