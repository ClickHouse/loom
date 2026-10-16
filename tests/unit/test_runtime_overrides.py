"""Runtime overrides live in loom.runtime_overrides: an override set through one
process binds every process that adopts the table, and outlives the process."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.core import runtime_overrides as ro
from server.core.ratelimit import Limit, Limiter
from server.observability.budgets import BudgetOverrides


@pytest.fixture(autouse=True)
def _no_own_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ro, "_own", {})


class _CH:
    """The table, with a server clock that ticks once per version read."""

    clock = 10**18

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        assert sql == ro._VERSION_SQL
        _CH.clock += 1
        return {"v": _CH.clock}

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        assert table == ro.TABLE and settings == {"async_insert": 0}
        for r in rows:
            self.rows[(r["kind"], r["key"])] = r

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "FINAL" in sql and "version" in sql
        return list(self.rows.values())


def test_put_clear_load_round_trip() -> None:
    ch = _CH()

    async def go() -> ro.Snapshot:
        await ro.put(ro.RATELIMIT_NAMESPACE, ("ns", "memory"), {"capacity": 1, "rate": 2}, ch=ch)
        await ro.put(ro.BUDGET_CONSUMER, ("bench", "*"), "3x", ch=ch)
        await ro.put(ro.BUDGET_OP, ("memory.get",), 50, ch=ch)
        await ro.clear(ro.BUDGET_OP, ("memory.get",), ch=ch)
        return await ro.load(ch=ch)

    snap = asyncio.run(go())
    assert snap == {
        ro.RATELIMIT_NAMESPACE: {("ns", "memory"): {"capacity": 1, "rate": 2}},
        ro.BUDGET_CONSUMER: {("bench", "*"): "3x"},
    }
    assert json.loads(ch.rows[(ro.BUDGET_OP, '["memory.get"]')]["value"]) is None


def test_the_limiter_adopts_overrides_and_resizes_live_buckets() -> None:
    lim = Limiter(clock=lambda: 0.0)
    assert lim.check("ns", "memory.get", org="o").allowed
    lim.adopt({ro.RATELIMIT_NAMESPACE: {("ns", "memory.read"): {"capacity": 1, "rate": 0.0}}})
    # The live bucket shrinks to the new capacity: one token left, then none.
    assert lim.check("ns", "memory.get", org="o").allowed
    assert not lim.check("ns", "memory.get", org="o").allowed
    lim.adopt({})
    assert lim._ns_overrides == {} and lim._org_overrides == {}


def test_the_limiter_adopts_org_overrides() -> None:
    lim = Limiter(clock=lambda: 0.0)
    lim.adopt({ro.RATELIMIT_ORG: {("o", "memory"): {"capacity": 2, "rate": 1}}})
    assert lim._org_overrides == {("o", "memory"): Limit(2.0, 1.0)}


def test_budgets_adopt_per_op_and_per_consumer_overrides() -> None:
    b = BudgetOverrides()
    b.adopt(
        {
            ro.BUDGET_OP: {("memory.get",): 40, ("memory.set",): -5, ("memory.ask",): "x"},
            ro.BUDGET_CONSUMER: {("bench", "*"): "3x", ("bad", "*"): "nonsense"},
        }
    )
    assert b.effective("memory.get", 100) == 40
    assert b.effective("memory.set", 100) == 100 and b.effective("memory.ask", 100) == 100
    assert b.effective("memory.search", 100, consumer="bench") == 300
    assert b.get_for_consumer("bad", "*") is None


async def test_every_process_adopts_the_table_at_boot_and_on_its_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read keeps the overrides already adopted."""
    from server import boot
    from server.core.ratelimit import get_limiter
    from server.settings import settings

    monkeypatch.setattr(settings, "runtime_overrides_refresh_seconds", 0.0)
    reads = [
        {ro.RATELIMIT_ORG: {("o", "memory"): {"capacity": 7, "rate": 1}}},
        ConnectionError("down"),
    ]
    adopted = asyncio.Event()

    async def load() -> ro.Snapshot:
        if not reads:
            adopted.set()
            await asyncio.Event().wait()
        r = reads.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ro, "load", load)
    task = boot.start_runtime_overrides()
    await asyncio.wait_for(adopted.wait(), 5)
    task.cancel()
    assert get_limiter()._org_overrides == {("o", "memory"): Limit(7.0, 1.0)}
    get_limiter().adopt({})


def test_a_cleared_override_returns_the_bucket_to_the_default_limit() -> None:
    lim = Limiter(clock=lambda: 0.0)
    lim.adopt({ro.RATELIMIT_NAMESPACE: {("ns", "memory.read"): {"capacity": 1, "rate": 0.0}}})
    assert lim.check("ns", "memory.get", org="o").limit == 1
    lim.adopt({})
    assert lim.check("ns", "memory.get", org="o").limit > 1


def test_an_unusable_row_is_skipped_and_the_rest_adopted() -> None:
    lim = Limiter(clock=lambda: 0.0)
    lim.adopt(
        {
            ro.RATELIMIT_NAMESPACE: {
                ("ns", "memory"): {"capacity": 2, "rate": 1},
                ("bad", "memory"): "3x",
                ("zero", "memory"): {"capacity": 0, "rate": 1},
            }
        }
    )
    assert lim._ns_overrides == {("ns", "memory"): Limit(2.0, 1.0)}


def test_a_read_that_began_before_this_processs_write_keeps_the_write() -> None:
    """The refresh would otherwise undo an override set here a moment ago."""
    ch = _CH()

    class _Stale(_CH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            rows = await ch.query_all(sql, params)
            await ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=ch)  # lands mid-read
            await ro.clear(ro.BUDGET_OP, ("memory.set",), ch=ch)
            return rows

    asyncio.run(ro.put(ro.BUDGET_OP, ("memory.set",), 9, ch=ch))
    snap = asyncio.run(ro.load(ch=_Stale()))
    assert snap == {ro.BUDGET_OP: {("memory.get",): 7}}
    assert asyncio.run(ro.load(ch=ch)) == {ro.BUDGET_OP: {("memory.get",): 7}}
    assert ro._own == {}  # both writes are in the table now


def test_a_lagging_replica_read_does_not_revert_this_processs_write() -> None:
    """A read that began after the write but hit a replica without it yet."""
    ch, lagging = _CH(), _CH()
    asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=ch))
    asyncio.run(ro.clear(ro.BUDGET_OP, ("memory.set",), ch=ch))
    lagging.rows = {k: v for k, v in ch.rows.items() if "memory.get" not in k[1]}
    lagging.rows[(ro.BUDGET_OP, '["memory.set"]')] = {
        "kind": ro.BUDGET_OP,
        "key": '["memory.set"]',
        "value": "9",
        "deleted": 0,
        "version": 1,
    }
    assert asyncio.run(ro.load(ch=lagging)) == {ro.BUDGET_OP: {("memory.get",): 7}}
    assert len(ro._own) == 2  # still waiting for a read that shows them
    assert asyncio.run(ro.load(ch=ch)) == {ro.BUDGET_OP: {("memory.get",): 7}}
    assert ro._own == {}


def test_a_newer_write_from_another_process_wins_over_this_processs_own() -> None:
    ch = _CH()
    asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=ch))
    newer = ro._own[(ro.BUDGET_OP, ("memory.get",))][1] + 1
    ch.rows[(ro.BUDGET_OP, '["memory.get"]')] = {
        "kind": ro.BUDGET_OP,
        "key": '["memory.get"]',
        "value": "11",
        "deleted": 0,
        "version": newer,
    }
    assert asyncio.run(ro.load(ch=ch)) == {ro.BUDGET_OP: {("memory.get",): 11}}
    assert ro._own == {}


def test_a_refused_insert_is_a_typed_error_and_changes_nothing() -> None:
    class _ReadOnly(_CH):
        async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None):
            raise RuntimeError("Code: 164. DB::Exception: Cannot execute query in readonly mode")

    with pytest.raises(ro.OverrideNotStoredError) as e:
        asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=_ReadOnly()))
    # Permanent: a 409 the SDK does not retry, not a 503.
    assert e.value.status_code == 409 and e.value.code == "OVERRIDE_NOT_STORED"
    assert "readonly" not in e.value.message and ro._own == {}


def test_an_override_on_a_live_bucket_is_the_replica_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every replica adopts it: each must hold its share, as a fresh bucket does."""
    from server.settings import settings

    monkeypatch.setattr(settings, "replica_count", 4)
    lim = Limiter(clock=lambda: 0.0)
    lim.check("ns", "memory.get", org="o")  # a live bucket
    lim.adopt({ro.RATELIMIT_NAMESPACE: {("ns", "memory.read"): {"capacity": 40, "rate": 4}}})
    live = lim.check("ns", "memory.get", org="o").limit
    lim.reset()
    fresh = lim.check("ns", "memory.get", org="o").limit
    assert live == fresh == 10


def test_an_unchanged_override_leaves_the_live_bucket_as_it_is() -> None:
    """Adopting the same snapshot again must not refill or resize anything."""
    lim = Limiter(clock=lambda: 0.0)
    lim.check("other", "memory.get", org="o")  # a bucket the override does not reach
    snap = {
        ro.RATELIMIT_NAMESPACE: {("ns", "memory.read"): {"capacity": 2, "rate": 0.0}},
        ro.RATELIMIT_ORG: {("o", "memory.read"): {"capacity": 50, "rate": 0.0}},
    }
    lim.adopt(snap)
    assert lim.check("ns", "memory.get", org="o").allowed
    lim.adopt(snap)
    assert lim.check("ns", "memory.get", org="o").allowed
    assert not lim.check("ns", "memory.get", org="o").allowed  # 2 spent, not 2 more


def test_a_cleared_override_resizes_only_its_own_buckets_and_refills_none() -> None:
    """Back at the default limit, a throttled tenant keeps the tokens it had."""
    lim = Limiter(clock=lambda: 0.0)
    lim.set_org_limit("o", "memory.read", Limit(1, 0.0))
    lim.set_namespace_limit("ns", "memory.read", Limit(1, 0.0))
    lim.set_namespace_limit("other", "memory.read", Limit(1, 0.0))
    for ns in ("ns", "other"):
        lim.check(ns, "memory.get", org="o")
    lim.clear_namespace_limit("ns", "memory.read")
    cleared, kept = (
        lim._ns_buckets[("o", "ns", "memory.read")],
        lim._ns_buckets[("o", "other", "memory.read")],
    )
    assert cleared.capacity > 1 and cleared.tokens < 1 and kept.capacity == 1
    lim.clear_org_limit("o", "memory.read")
    org = lim._org_buckets[("o", "memory.read")]
    assert org.capacity > 1 and org.tokens < 1
    assert not lim.check("ns", "memory.get", org="o").allowed


def test_a_malformed_row_is_skipped_and_the_rest_adopted(caplog: pytest.LogCaptureFixture) -> None:
    """One row that does not parse must not stop every later refresh."""
    ch = _CH()
    asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=ch))
    ro._own.clear()
    bad = {"kind": ro.BUDGET_OP, "key": "not json", "value": "1", "deleted": 0, "version": 5}
    worse = {"kind": ro.BUDGET_OP, "key": '["x"]', "value": "{", "deleted": 0, "version": 6}
    ch.rows[("bad", "1")], ch.rows[("bad", "2")] = bad, worse
    with caplog.at_level("WARNING", logger="loom.runtime_overrides"):
        assert asyncio.run(ro.load(ch=ch)) == {ro.BUDGET_OP: {("memory.get",): 7}}
        assert asyncio.run(ro.load(ch=ch)) == {ro.BUDGET_OP: {("memory.get",): 7}}
    assert sum("does not parse" in r.getMessage() for r in caplog.records) == 2  # once per row


def test_the_version_is_the_servers_clock_not_this_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose clock runs ahead must not make an earlier set outlive a later clear."""
    ch = _CH()
    monkeypatch.setattr(ro.time, "time_ns", lambda: 10**19)  # this host: far ahead
    asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=ch))
    set_version = ch.rows[(ro.BUDGET_OP, '["memory.get"]')]["version"]
    assert set_version < 10**19
    # Another host clears it a moment later on the server's clock.
    ch.rows[(ro.BUDGET_OP, '["memory.get"]')] = {
        "kind": ro.BUDGET_OP,
        "key": '["memory.get"]',
        "value": "null",
        "deleted": 1,
        "version": set_version + 1,
    }
    assert asyncio.run(ro.load(ch=ch)) == {}
    assert ro._own == {}  # the overlay is keyed on the server version the clear beat


def test_one_processs_writes_never_tie_on_a_coarse_server_clock() -> None:
    class _Coarse(_CH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"v": 5}

    ch = _Coarse()
    asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=ch))
    first = ro._own[(ro.BUDGET_OP, ("memory.get",))][1]
    asyncio.run(ro.clear(ro.BUDGET_OP, ("memory.get",), ch=ch))
    assert ro._own[(ro.BUDGET_OP, ("memory.get",))][1] > first


def test_a_failed_write_that_may_succeed_on_retry_is_a_503() -> None:
    from server.core.errors import BackendUnavailableError

    class _Busy(_CH):
        async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None):
            raise RuntimeError("Code: 252. DB::Exception: Too many parts")

    with pytest.raises(BackendUnavailableError) as e:
        asyncio.run(ro.put(ro.BUDGET_OP, ("memory.get",), 7, ch=_Busy()))
    assert e.value.status_code == 503 and ro._own == {}


def test_an_unusable_override_warns_once_not_every_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lim = Limiter(clock=lambda: 0.0)
    snap = {ro.RATELIMIT_NAMESPACE: {("bad", "memory"): "3x"}}
    with caplog.at_level("WARNING", logger="loom.ratelimit"):
        for _ in range(3):
            lim.adopt(snap)
        lim.adopt({ro.RATELIMIT_NAMESPACE: {("bad", "memory"): "4x"}})  # a new row: said again
    assert sum("unusable" in r.getMessage() for r in caplog.records) == 2
