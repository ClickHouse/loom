"""ClickHouse client fault handling, run against a live ClickHouse through a
TCP proxy that injects faults between loom's client and the server.

- A socket dropped after the server has committed an INSERT: the driver
  (and loom) retry the batch; with the per-batch deduplication token the
  server keeps one copy — `count() == uniqExact(request_id, operation)`.
- A statement whose response never arrives: the read-class deadline cuts
  it, the pool slot is released, the client keeps working.

Skips cleanly when LOOM_CH_HOST is unset. Plain MergeTree keeps no block
tokens unless `non_replicated_deduplication_window` is set: migration 032
sets 1000 on every MergeTree table (Cloud's SharedMergeTree keeps 10,000 by
default), so the dedup case skips only on an unmigrated table without one.

The control arm (token off) is not a CI test: a server with a window
deduplicates identical blocks by content hash regardless of
`insert_deduplicate = 0` (on the 26.5 CI image and on Cloud 26.8), so the
retried batch cannot land twice there either; its
duplicate is real only on a server without a window. `proxy.drops == 1`
together with a successful `insert()` is the proof the retry happened.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

pytestmark = [pytest.mark.usefixtures("ch_target_required"), pytest.mark.recovery]

_TABLE = "loom.agent_events"
_OP = "itest.fault-3720"


class _FaultProxy:
    """TCP relay 127.0.0.1:<port> → upstream. `mode`: `pass` relays;
    `drop_response` relays the request, then closes the client side the
    moment the first response byte arrives (once, then back to `pass`);
    `stall` relays the request and never relays the response."""

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.mode = "pass"
        self.drops = 0
        self.stalled = 0
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._stalls: list[asyncio.Event] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for ev in self._stalls:
            ev.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection(*self.upstream)
        except OSError:
            cw.close()
            return

        async def c2u() -> None:
            try:
                while True:
                    data = await cr.read(65536)
                    if not data:
                        break
                    uw.write(data)
                    await uw.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    uw.write_eof()
                except Exception:  # noqa: BLE001 — peer may be gone
                    pass

        async def u2c() -> None:
            try:
                while True:
                    data = await ur.read(65536)
                    if not data:
                        break
                    if self.mode == "drop_response":
                        self.mode = "pass"
                        self.drops += 1
                        cw.transport.abort()  # RST: the client sees a peer close
                        return
                    if self.mode == "stall":
                        self.stalled += 1
                        ev = asyncio.Event()
                        self._stalls.append(ev)
                        await ev.wait()  # hold the response until told otherwise
                        return
                    cw.write(data)
                    await cw.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass

        t1 = asyncio.create_task(c2u())
        t2 = asyncio.create_task(u2c())
        try:
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t1, t2):
                t.cancel()
            for w in (cw, uw):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass


@pytest.fixture
async def proxy() -> Any:
    from server.settings import settings

    p = _FaultProxy(settings.ch_host, int(settings.ch_port))
    await p.start()
    yield p
    await p.stop()


@pytest.fixture
async def direct() -> Any:
    """A client straight at the server, for the counts."""
    from server.core.ch import CHClient

    c = CHClient()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
async def through_proxy(proxy: _FaultProxy, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh CHClient whose only route to the server is the proxy."""
    from server.core.ch import CHClient, describe_cache_clear
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "127.0.0.1")
    monkeypatch.setattr(settings, "ch_port", proxy.port)
    monkeypatch.setattr(settings, "ch_secure", False)
    describe_cache_clear()
    c = CHClient()
    await c.connect()
    yield c
    await c.disconnect()
    describe_cache_clear()


def _rows(n: int, namespace: str) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [
        {
            "ts": now,
            "org": "itest-org",
            "namespace": namespace,
            "agent": "itest-bot",
            "user_id": "-",
            "session_id": "-",
            "operation": _OP,
            "request_id": uuid.uuid4().hex,
            "latency_ms": 1,
            "success": True,
        }
        for _ in range(n)
    ]


async def _dedup_window(direct: Any) -> int:
    """The table's own window where its definition sets one (migration 032
    does), else the server default."""
    eng = await direct.query_one(
        "SELECT engine, engine_full FROM system.tables "
        "WHERE database = 'loom' AND name = 'agent_events'"
    )
    engine = str((eng or {}).get("engine") or "")
    name = (
        "replicated_deduplication_window"
        if "Replicated" in engine or "Shared" in engine
        else "non_replicated_deduplication_window"
    )
    if own := re.search(rf"\b{name} = (\d+)", str((eng or {}).get("engine_full") or "")):
        return int(own.group(1))
    row = await direct.query_one(
        "SELECT value FROM system.merge_tree_settings WHERE name = {n:String}", {"n": name}
    )
    return int((row or {}).get("value") or 0)


async def _count(direct: Any, namespace: str) -> tuple[int, int]:
    from server.core.ch import sequential_reads

    with sequential_reads():
        row = await direct.query_one(
            "SELECT count() AS n, uniqExact(request_id, operation) AS u FROM loom.agent_events "
            "WHERE namespace = {ns:String} AND operation = {op:String}",
            {"ns": namespace, "op": _OP},
        )
    return int(row["n"]), int(row["u"])


async def test_a_socket_dropped_after_commit_lands_the_batch_once(
    proxy: _FaultProxy, direct: Any, through_proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if await _dedup_window(direct) <= 0:
        pytest.skip("this server keeps no insert block tokens (deduplication window 0)")
    ns = f"itest-3720-{uuid.uuid4().hex[:10]}"
    # Warm the column-type cache so the armed drop hits the INSERT, not the
    # DESCRIBE that precedes a first insert.
    assert await through_proxy._live_column_types(_TABLE)
    rows = _rows(3, ns)
    proxy.mode = "drop_response"
    t0 = time.perf_counter()
    await through_proxy.insert(
        _TABLE, rows, settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
    wall = time.perf_counter() - t0
    assert proxy.drops == 1, "the fault was not injected"
    n, u = await _count(direct, ns)
    assert (n, u) == (3, 3), f"duplicates under injection: count={n} uniq={u} (wall {wall:.2f}s)"
    # Hygiene: the rows are telemetry about this test; remove them.
    await direct.execute("DELETE FROM loom.agent_events WHERE namespace = {ns:String}", {"ns": ns})


async def test_a_hung_statement_releases_its_slot_at_the_class_deadline(
    proxy: _FaultProxy, through_proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.ch import pool_reading
    from server.core.errors import BackendTimeoutError
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_timeout_read_s", 1.0)
    await through_proxy.query_all("SELECT 1")  # the route works
    proxy.mode = "stall"
    t0 = time.perf_counter()
    with pytest.raises(BackendTimeoutError) as ei:
        await through_proxy.query_all("SELECT 2")
    waited = time.perf_counter() - t0
    assert proxy.stalled == 1
    assert 0.9 <= waited < 3.0, f"deadline fired at {waited:.2f}s, bound 1.0s"
    assert ei.value.to_detail()["statement_class"] == "read"
    r = pool_reading()
    assert r["in_use"] == 0 and r["waiters"] == 0
    proxy.mode = "pass"
    # The client is intact: the next statement runs on a fresh connection.
    assert await through_proxy.query_all("SELECT 3 AS x") == [{"x": 3}]
