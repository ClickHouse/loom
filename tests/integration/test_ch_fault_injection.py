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
import hashlib
import ipaddress
import json
import os
import re
import signal
import ssl
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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

    def __init__(
        self, upstream_host: str, upstream_port: int, *, tls: ssl.SSLContext | None = None
    ) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.mode = "pass"
        self.drops = 0
        self.drop_remaining = 1
        self.stalled = 0
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._stalls: list[asyncio.Event] = []
        self._tls = tls
        self.tls_connections = 0
        self._handlers: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._stopping = False
        self._errors: list[BaseException] = []
        self.parser_headers = asyncio.Event()
        self.parser_release = asyncio.Event()
        self.parser_marker = b""
        self.parser_handler: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, ssl=self._tls)
        self.port = self._server.sockets[0].getsockname()[1]

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        if task.cancelled():
            self._errors.append(asyncio.CancelledError("relay handler was cancelled"))
        elif error := task.exception():
            self._errors.append(error)

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for ev in self._stalls:
            ev.set()
        for writer in tuple(self._writers):
            writer.close()
        if self._handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(self._handlers)), timeout=5)
        assert not self._handlers and not self._writers, "relay ownership did not drain"
        assert not self._errors, self._errors

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        owner = asyncio.current_task()
        parser_target = False
        writers, children = [cw], []
        self._writers.add(cw)
        self.tls_connections += int(cw.get_extra_info("ssl_object") is not None)
        try:
            if self._stopping:
                return
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 5)
            except OSError:
                return
            writers.append(uw)
            self._writers.add(uw)
            if self._stopping:
                return

            async def c2u() -> None:
                nonlocal parser_target
                tail = b""
                try:
                    while data := await cr.read(65536):
                        if self.parser_marker:
                            scan = tail + data
                            parser_target = parser_target or self.parser_marker in scan
                            tail = scan[-len(self.parser_marker) :]
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
                    while data := await ur.read(65536):
                        if self.mode == "parser-body" and parser_target:
                            # Real headers start the installed parser; real body bytes stay withheld.
                            pending = bytearray(data)
                            while b"\r\n\r\n" not in pending:
                                assert len(pending) < 65536, (
                                    "response headers exceeded finite bound"
                                )
                                more = await ur.read(4096)
                                assert more, "upstream closed before HTTP headers"
                                pending.extend(more)
                            split = pending.index(b"\r\n\r\n") + 4
                            self.parser_handler = owner
                            cw.write(pending[:split])
                            await cw.drain()
                            self.parser_headers.set()
                            await self.parser_release.wait()
                            cw.write(pending[split:])
                            await cw.drain()
                            self.mode = "pass"
                            continue
                        if self.mode == "drop_response":
                            self.drop_remaining -= 1
                            if self.drop_remaining == 0:
                                self.mode = "pass"
                                self.drop_remaining = 1
                            self.drops += 1
                            cw.transport.abort()
                            return
                        if self.mode == "stall":
                            self.stalled += 1
                            ev = asyncio.Event()
                            self._stalls.append(ev)
                            await ev.wait()
                            return
                        cw.write(data)
                        await cw.drain()
                except (ConnectionError, asyncio.CancelledError):
                    pass

            children = [asyncio.create_task(c2u()), asyncio.create_task(u2c())]
            await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for child in children:
                child.cancel()
            outcomes = await asyncio.gather(*children, return_exceptions=True)
            for writer in writers:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
            self._writers.difference_update(writers)
            for result in outcomes:
                if isinstance(result, BaseException) and not isinstance(
                    result, (asyncio.CancelledError, OSError)
                ):
                    raise result


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


def _vector_tls(tmp_path: Path) -> tuple[ssl.SSLContext, Path]:
    """A fixture-owned certificate trusted by the actual driver's default context."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "relay.crt", tmp_path / "relay.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context, cert_file


@pytest.fixture(params=[False, True], ids=["http", "verified-https"])
async def vector_native(request, monkeypatch, tmp_path):
    from server.core import ch, request_work
    from server.core.vectors import admission
    from server.settings import settings
    from tests.integration._resources import unique_org

    for name, value in {
        "ch_pool_maxsize": 4,
        "ch_pool_wait_ms": 500,
        "ch_timeout_read_s": 3.0,
        "ch_caller_shaped_max_execution_s": 3.0,
        "vector_scan_max_concurrent_per_org": 2,
        "vector_scan_max_waiters_per_org": 4,
        "vector_scan_max_rows_to_read": 1_000_000,
    }.items():
        monkeypatch.setattr(settings, name, value)
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    direct, client = ch.CHClient(), None
    relay = None
    try:
        with admission.vector_work("", (), 0), request_work.bind(request_work.RequestWork()):
            await direct.connect()
            tls, cert_file = _vector_tls(tmp_path) if request.param else (None, None)
            if cert_file is not None:
                monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
            relay = _FaultProxy(settings.ch_host, int(settings.ch_port), tls=tls)
            await relay.start()
            monkeypatch.setattr(settings, "ch_host", "127.0.0.1")
            monkeypatch.setattr(settings, "ch_port", relay.port)
            monkeypatch.setattr(settings, "ch_secure", request.param)
            client = ch.CHClient()
            await client.connect()
            assert await client.query_one("SELECT 1 AS ready") == {"ready": 1}
            if request.param:
                context = client.client._backend.ssl_context
                assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
                assert relay.tls_connections > 0
        yield SimpleNamespace(
            client=client,
            direct=direct,
            relay=relay,
            gate=gate,
            hot=unique_org("vector-hot"),
            light=unique_org("vector-light"),
        )
    finally:
        with admission.vector_work("", (), 0):
            try:
                if client is not None:
                    await client.disconnect()
            finally:
                try:
                    if relay is not None:
                        await relay.stop()
                finally:
                    await direct.disconnect()
        assert gate.population == 0 and gate.orgs == {}


async def _vector_call(client, org, request_id, sql, *, weight=1):
    from server.core import ch, request_work
    from server.core.vectors import admission

    with (
        request_work.bind(request_work.RequestWork()),
        ch.query_tag("itest.vector-native " + request_id),
        admission.vector_work(org, "native", weight),
    ):
        return await client.query_all(sql)


async def _vector_running(direct, request_id):
    from server.core import ch
    from server.core.vectors import admission

    with admission.vector_work("", (), 0), ch.query_tag("itest.vector-observer"):
        rows = await direct.query_all(
            "SELECT query_id, elapsed FROM system.processes "
            "WHERE startsWith(query_id, {prefix:String}) AND query_id != queryID() LIMIT 16",
            {"prefix": request_id + ":"},
        )
        assert len(rows) < 16, "owned running-query census reached its finite bound"
        return rows


async def _vector_remote_end(direct, request_id, initially_seen):
    """Positive native terminal events for every observed start, not one empty sample."""
    from server.core import ch
    from server.core.vectors import admission

    with admission.vector_work("", (), 0), ch.query_tag("itest.vector-observer"):
        defaults = {
            row["name"]: row["default"]
            for row in await direct.query_all(
                "SELECT name, default FROM system.settings WHERE name IN "
                "('max_rows_to_read','max_memory_usage','max_execution_time','read_overflow_mode')"
            )
        }
        deadline = time.monotonic() + 12
        peak, samples = len(initially_seen), 0
        seen = set(initially_seen)
        while True:
            active = await _vector_running(direct, request_id)
            seen.update(row["query_id"] for row in active)
            peak, samples = max(peak, len(active)), samples + 1
            await direct.execute("SYSTEM FLUSH LOGS")
            events = await direct.query_all(
                "SELECT query_id, toString(type) AS event, exception_code, "
                "toUnixTimestamp64Micro(event_time_microseconds) AS at_us, "
                "Settings['max_rows_to_read'] AS rows_cap, "
                "Settings['max_memory_usage'] AS memory_cap, "
                "Settings['max_execution_time'] AS time_cap, "
                "Settings['read_overflow_mode'] AS overflow "
                "FROM system.query_log WHERE startsWith(query_id, {prefix:String}) "
                "AND is_initial_query ORDER BY event_time_microseconds, event LIMIT 64",
                {"prefix": request_id + ":"},
            )
            assert len(events) < 64, "owned attempt evidence exceeded its finite census"
            starts = [row for row in events if row["event"] == "QueryStart"]
            terminal = [
                row
                for row in events
                if row["event"]
                in ("QueryFinish", "ExceptionWhileProcessing", "ExceptionBeforeStart")
            ]
            seen.update(row["query_id"] for row in events)
            ended = {row["query_id"] for row in terminal}
            for row in starts:
                for field, name in {
                    "rows_cap": "max_rows_to_read",
                    "memory_cap": "max_memory_usage",
                    "time_cap": "max_execution_time",
                    "overflow": "read_overflow_mode",
                }.items():
                    row[field] = row[field] or defaults[name]
                assert float(row["rows_cap"]) == 1_000_000 and row["overflow"] == "throw"
                assert 0 < float(row["memory_cap"]) <= 4_000_000_000
                assert 0 < float(row["time_cap"]) <= 3
            if starts and not active and seen <= ended and len(terminal) >= len(starts):
                print(
                    "vector-native-lifetime "
                    + json.dumps(
                        {
                            "request_id": request_id,
                            "peak_observed_remote": peak,
                            "samples": samples,
                            "events": events,
                        },
                        sort_keys=True,
                    )
                )
                return events
            assert time.monotonic() < deadline, {
                "active": active,
                "seen": sorted(seen),
                "events": events,
            }
            await asyncio.sleep(0.05)


async def _vector_cleanup(direct, request_id, failure):
    """Stop only exact owned queries, preserving a failed case over a cleanup error."""
    from server.core import ch
    from server.core.vectors import admission

    try:
        with admission.vector_work("", (), 0), ch.query_tag("itest.vector-cleanup"):
            for row in await _vector_running(direct, request_id):
                query_id = row["query_id"]
                assert query_id.startswith(request_id + ":")
                await direct.execute(
                    "KILL QUERY WHERE query_id = {owned:String} SYNC", {"owned": query_id}
                )
            assert not await _vector_running(direct, request_id)
    except Exception as error:  # Keep the first failed assertion as the result.
        if failure is None:
            raise
        failure.add_note("owned vector cleanup also failed: " + type(error).__name__)


def _vector_drained(state):
    from server.core import ch

    assert state.gate.population == 0 and state.gate.orgs == {}
    assert state.client._slots._value == 4
    assert state.client._inflight == {} and state.client._retired == {}
    assert ch.pool_reading()["in_use"] == 0 and ch.pool_reading()["waiters"] == 0


@pytest.mark.parametrize("outcome", ["cancel", "deadline", "stalled-response"])
async def test_native_vector_failure_drains_local_work_and_observes_remote_end(
    vector_native, monkeypatch, outcome
):
    from server.core.errors import VectorAdmissionError
    from server.settings import settings

    state = vector_native
    request_id = "vector-held-" + uuid.uuid4().hex
    # The server executes a finite ~2 s block; local cancellation is not a remote ack.
    sql = (
        "SELECT sum(number) AS total, "
        "sum(cosineDistance([toFloat32(number + 1), 1.0], [1.0, 0.0])) AS score "
        "FROM numbers(100) WHERE sleepEachRow(0.02) = 0"
    )
    if outcome == "deadline":
        monkeypatch.setattr(settings, "ch_timeout_read_s", 1.0)
    caller = asyncio.create_task(_vector_call(state.client, state.hot, request_id, sql, weight=2))
    queued = None
    try:
        deadline = time.monotonic() + 2
        while not (active := await _vector_running(state.direct, request_id)):
            assert not caller.done(), "native held query ended before the observation"
            assert time.monotonic() < deadline, "native held query never appeared"
            await asyncio.sleep(0.01)
        assert len(active) == 1
        assert state.gate.population == 2 and state.gate.orgs[state.hot].held == 2
        queued = asyncio.create_task(
            _vector_call(
                state.client,
                state.hot,
                "vector-queued-" + uuid.uuid4().hex,
                "SELECT cosineDistance([1.0, 0.0], [1.0, 0.0]) AS score",
            )
        )
        await asyncio.sleep(0)
        assert state.gate.orgs[state.hot].waiting == 1 and state.gate.population == 3
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert state.gate.orgs[state.hot].waiting == 0 and state.gate.population == 2
        light_id = "vector-light-" + uuid.uuid4().hex
        light = await _vector_call(
            state.client,
            state.light,
            light_id,
            "SELECT cosineDistance([1.0, 0.0], [1.0, 0.0]) AS score, 7 AS complete",
        )
        assert light == [{"score": 0.0, "complete": 7}]
        overlap = await _vector_running(state.direct, request_id)
        assert overlap and not caller.done(), (
            "the light org must finish while hot native work remains"
        )
        assert state.gate.population == 2 and state.gate.orgs[state.hot].held == 2
        if outcome == "stalled-response":
            state.relay.mode = "stall"
        if outcome == "cancel":
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        else:
            with pytest.raises(VectorAdmissionError) as caught:
                await caller
            assert caught.value.extra["reason"] == "deadline"
        if outcome == "stalled-response":
            assert state.relay.stalled == 1, "the attributed response was not stalled"
            state.relay.mode = "pass"
        _vector_drained(state)
        assert (
            await _vector_call(
                state.client,
                state.light,
                "vector-reused-" + uuid.uuid4().hex,
                "SELECT cosineDistance([1.0, 0.0], [1.0, 0.0]) AS score, 7 AS complete",
            )
            == light
        )
        after_local = await _vector_running(state.direct, request_id)
        print(
            "vector-native-competing-progress "
            + json.dumps(
                {
                    "outcome": outcome,
                    "hot_query_ids_at_light_completion": [row["query_id"] for row in overlap],
                    "remote_after_local_failure": after_local,
                    "light_complete": light,
                },
                sort_keys=True,
            )
        )
        events = await _vector_remote_end(
            state.direct, request_id, [row["query_id"] for row in active]
        )
        assert events and all(row["query_id"].startswith(request_id + ":") for row in events)
        await _vector_remote_end(state.direct, light_id, [])
        _vector_drained(state)
    finally:
        state.relay.mode = "pass"
        for task in (caller, queued):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (caller, queued) if task is not None), return_exceptions=True
        )
        await _vector_cleanup(state.direct, request_id, sys.exception())


@pytest.mark.parametrize("retry_layer", ["driver", "core"])
async def test_native_vector_remote_close_correlates_installed_retries(vector_native, retry_layer):
    state = vector_native
    request_id = "vector-retry-" + uuid.uuid4().hex
    sql = "SELECT cosineDistance([1.0, 0.0], [1.0, 0.0]) AS score, 7 AS complete"
    control = await _vector_call(state.client, state.hot, "vector-control-" + uuid.uuid4().hex, sql)
    generation = state.client.generation
    hidden_attempts = max(2, state.client.client.query_retries + 1)
    drops = 1 if retry_layer == "driver" else hidden_attempts
    try:
        state.relay.drop_remaining = drops
        state.relay.mode = "drop_response"
        result = await _vector_call(state.client, state.hot, request_id, sql)
        assert result == control == [{"score": 0.0, "complete": 7}]
        assert state.relay.drops == drops
        assert state.client.generation == generation + int(retry_layer == "core")
        events = await _vector_remote_end(state.direct, request_id, [])
        starts = [row for row in events if row["event"] == "QueryStart"]
        finishes = [row for row in events if row["event"] == "QueryFinish"]
        assert len(starts) == len(finishes) == drops + 1
        identities = {row["query_id"] for row in starts}
        assert len(identities) == (1 if retry_layer == "driver" else 2)
        if retry_layer == "core":
            counts = sorted(sum(row["query_id"] == qid for row in starts) for qid in identities)
            assert counts == [1, hidden_attempts]
        assert all(row["exception_code"] == 0 for row in finishes)
        print(
            "vector-native-retry-policy "
            + json.dumps(
                {
                    "layer": retry_layer,
                    "query_retries": state.client.client.query_retries,
                    "dropped_responses": drops,
                    "native_executions": len(starts),
                    "native_identities": sorted(identities),
                    "generation_change": state.client.generation - generation,
                },
                sort_keys=True,
            )
        )
        _vector_drained(state)
    finally:
        state.relay.mode = "pass"
        await _vector_cleanup(state.direct, request_id, sys.exception())


def _native_parser_stack(thread_id, source):
    """Observe the real executor parser waiting on this response's normal byte queue."""
    frame = sys._current_frames().get(thread_id)
    found = {"parser": False, "queue_get": False, "condition_wait": False}
    while frame is not None:
        name, filename = frame.f_code.co_name, frame.f_code.co_filename
        if name == "parse_streaming" and frame.f_locals.get("streaming_source") is source:
            found["parser"] = True
        receiver = frame.f_locals.get("self")
        if (
            name == "get"
            and filename.endswith("/clickhouse_connect/driver/asyncqueue.py")
            and getattr(receiver, "_p", None) is source.queue
        ):
            found["queue_get"] = True
        if name == "wait" and receiver is source.queue._sync_not_empty:
            found["condition_wait"] = True
        frame = frame.f_back
    return found


async def test_native_parser_executor_retires_after_caller_cancellation(vector_native, monkeypatch):
    """Cancel a real blocked parser, then prove its concurrent executor work actually ended."""
    state = vector_native
    request_id = "vector-parser-" + uuid.uuid4().hex
    marker = "/* " + request_id + " */"
    query = (
        "SELECT number, cosineDistance([toFloat32(number + 1), 1.0], [1.0, 0.0]) AS score "
        "FROM numbers(3) "
    )
    control = await _vector_call(
        state.client, state.hot, "vector-parser-control-" + uuid.uuid4().hex, query
    )
    assert len(control) == 3 and [r["number"] for r in control] == [0, 1, 2]
    loop = asyncio.get_running_loop()
    executor = loop._default_executor
    assert isinstance(executor, ThreadPoolExecutor), "actual driver parser executor unavailable"
    submit = executor.submit
    calls = []

    def observed_submit(function, *args, **kwargs):
        code = getattr(function, "__code__", None)
        if code is None or code.co_name != "parse_streaming":
            return submit(function, *args, **kwargs)
        closure = dict(
            zip(code.co_freevars, (c.cell_contents for c in function.__closure__), strict=True)
        )
        context = closure.get("context")
        if marker not in str(getattr(context, "query", "")):
            return submit(function, *args, **kwargs)
        assert not calls, "unexpected repeated parser submission for the held native response"
        record = {
            "source": closure["streaming_source"],
            "entered": threading.Event(),
            "retired": threading.Event(),
            "thread_id": None,
        }
        calls.append(record)

        def delegate():
            record["thread_id"] = threading.get_ident()
            record["entered"].set()
            try:
                return function(*args, **kwargs)
            finally:
                record["retired"].set()

        # Observe the actual concurrent Future; do not replace the driver parser/executor.
        record["future"] = submit(delegate)
        return record["future"]

    monkeypatch.setattr(executor, "submit", observed_submit)
    state.relay.parser_marker = marker.encode()
    state.relay.mode = "parser-body"
    caller = asyncio.create_task(
        _vector_call(state.client, state.hot, request_id, query + marker, weight=2)
    )
    try:
        deadline = time.monotonic() + 2
        while True:
            assert not caller.done(), "caller ended before an actual parser read was observed"
            if calls and calls[0]["entered"].is_set():
                record = calls[0]
                blocked = _native_parser_stack(record["thread_id"], record["source"])
                if all(blocked.values()):
                    break
            assert time.monotonic() < deadline, "actual executor parser never blocked on real bytes"
            await asyncio.sleep(0.01)
        assert state.relay.parser_headers.is_set() and not state.relay.parser_release.is_set()
        assert record["future"].running() and not record["retired"].is_set()
        assert state.gate.population == 2 and state.gate.orgs[state.hot].held == 2
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        deadline = time.monotonic() + 2
        while not record["future"].done() or not record["retired"].is_set():
            assert time.monotonic() < deadline, "canceled await left actual executor work running"
            await asyncio.sleep(0.01)
        assert not record["future"].cancelled(), "canceled Future is not executor-retirement proof"
        assert not _native_parser_stack(record["thread_id"], record["source"])["parser"]
        source = record["source"]
        assert source.queue._shutdown and source._producer_task.done()
        assert source.response.closed and source._lease_released
        # Cancellation, not releasing bytes, must have retired the real thread's work.
        assert not state.relay.parser_release.is_set()
        handler = state.relay.parser_handler
        assert handler is not None
        deadline = time.monotonic() + 2
        while not handler.done() or handler in state.relay._handlers:
            assert time.monotonic() < deadline, (
                "owned parser relay did not close after cancellation"
            )
            await asyncio.sleep(0.01)
        assert handler.exception() is None
        _vector_drained(state)
        events = await _vector_remote_end(state.direct, request_id, [])
        starts = [r for r in events if r["event"] == "QueryStart"]
        ends = [r for r in events if r["event"] == "QueryFinish"]
        assert len(starts) == len(ends) == 1 and ends[0]["exception_code"] == 0
        state.relay.mode = "pass"
        state.relay.parser_release.set()
        reused_id = "vector-parser-reused-" + uuid.uuid4().hex
        assert await _vector_call(state.client, state.light, reused_id, query) == control
        await _vector_remote_end(state.direct, reused_id, [])
        _vector_drained(state)
        error = record["future"].exception()
        print(
            "vector-native-parser-retirement "
            + json.dumps(
                {
                    "request_id": request_id,
                    "native_query_ids": [r["query_id"] for r in starts],
                    "executor": type(executor).__name__,
                    "thread_id": record["thread_id"],
                    "observed_real_blocked_stack": blocked,
                    "concurrent_future_done": record["future"].done(),
                    "concurrent_future_cancelled": record["future"].cancelled(),
                    "delegate_retired": record["retired"].is_set(),
                    "delegate_error": type(error).__name__ if error else None,
                    "response_producer_done": source._producer_task.done(),
                    "response_lease_released": source._lease_released,
                    "held_relay_done": handler.done(),
                    "complete_reuse": control,
                },
                sort_keys=True,
            )
        )
    finally:
        failure = sys.exception()
        monkeypatch.setattr(executor, "submit", submit)
        state.relay.mode = "pass"
        state.relay.parser_release.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        try:
            if calls and (pending_future := calls[0].get("future")) is not None:
                deadline = time.monotonic() + 2
                while not pending_future.done():
                    if time.monotonic() >= deadline:
                        if failure is not None:
                            failure.add_note(
                                "owned parser work did not retire during finite cleanup"
                            )
                            break
                        raise AssertionError(
                            "owned parser work did not retire during finite cleanup"
                        )
                    await asyncio.sleep(0.01)
                if pending_future.done() and not pending_future.cancelled():
                    pending_future.exception()
        finally:
            await _vector_cleanup(state.direct, request_id, failure or sys.exception())


# One finite native client, not a serving web worker or a reusable command server.
_VECTOR_RESTART_CHILD = """
import asyncio, hashlib, json, os, ssl, sys, time
from pathlib import Path
from types import SimpleNamespace
from server.core import ch
from server.core.vectors import admission
from server.settings import settings
from tests.integration.test_ch_fault_injection import (
    _vector_call, _vector_drained, _vector_running,
)

def note(kind, **fields):
    print(json.dumps({"kind": kind, **fields}, sort_keys=True), flush=True)

async def main(payload):
    for name, value in payload["settings"].items():
        setattr(settings, name, value)
    client = ch.CHClient()
    caller = None
    try:
        await client.connect()
        assert await client.query_one("SELECT 1 AS ready") == {"ready": 1}
        verified = False
        if settings.ch_secure:
            context = client.client._backend.ssl_context
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            verified = True
        stat = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        note("ready", pid=os.getpid(), uid=os.getuid(), parent=os.getppid(),
             start_ticks=stat[19], settings=payload["settings"], verified_tls=verified,
             query_retries=client.client.query_retries,
             core_sha256=hashlib.sha256(Path(ch.__file__).read_bytes()).hexdigest())
        deadline = time.monotonic() + 10
        while not Path(payload["start_file"]).exists():
            assert time.monotonic() < deadline, "own start barrier was not released"
            await asyncio.sleep(0.01)
        request_id = payload["request_id"]
        caller = asyncio.create_task(_vector_call(
            client, payload["hot"], request_id, payload["query"], weight=2
        ))
        deadline = time.monotonic() + 2
        while not (active := await _vector_running(client, request_id)):
            assert not caller.done(), "held native read ended before observation"
            assert time.monotonic() < deadline, "held native read never started"
            await asyncio.sleep(0.01)
        gate = admission._admission
        assert len(active) == 1 and gate.population == 2 and gate.orgs[payload["hot"]].held == 2
        note("held", query_ids=[r["query_id"] for r in active], population=gate.population,
             held=gate.orgs[payload["hot"]].held, pool_in_use=ch.pool_reading()["in_use"])
        light = await _vector_call(
            client, payload["light"], request_id + "-light",
            "SELECT cosineDistance([1.0, 0.0], [1.0, 0.0]) AS score, 7 AS complete",
        )
        assert light == [{"score": 0.0, "complete": 7}]
        active = await _vector_running(client, request_id)
        assert active and not caller.done() and gate.orgs[payload["hot"]].held == 2
        note("light", result=light, hot_query_ids=[r["query_id"] for r in active])
        result = await caller
        assert len(result) == 1 and result[0]["total"] == 6
        _vector_drained(SimpleNamespace(client=client, gate=gate))
        note("drained", result=result, population=gate.population, orgs=gate.orgs,
             pool=ch.pool_reading(), generation=client.generation,
             inflight=client._inflight, retired=client._retired)
    finally:
        if caller is not None and not caller.done():
            caller.cancel()
        if caller is not None:
            await asyncio.gather(caller, return_exceptions=True)
        await client.disconnect()

asyncio.run(main(json.loads(sys.argv[1])))
"""


def _native_child_identity(process):
    stat = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
    identity = {
        "pid": process.pid,
        "uid": Path(f"/proc/{process.pid}").stat().st_uid,
        "parent": int(stat[1]),
        "start_ticks": stat[19],
    }
    assert identity["uid"] == os.getuid() and identity["parent"] == os.getpid()
    return identity


async def _native_child_note(process, expected):
    line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
    assert line and len(line) < 16384, "own child exited or exceeded its finite receipt bound"
    note = json.loads(line)
    assert note.get("kind") == expected, note
    return note


async def test_native_process_death_restart_keeps_local_caps_and_observes_remote_work(
    vector_native, tmp_path
):
    from server.core import ch
    from server.settings import settings
    from tests.integration._redact import redact
    from tests.integration._resources import ch_target

    ch_target()  # The child inherits the explicitly configured CI store's credentials.
    state = vector_native
    names = (
        "ch_host",
        "ch_port",
        "ch_secure",
        "ch_pool_maxsize",
        "ch_pool_wait_ms",
        "ch_timeout_read_s",
        "ch_caller_shaped_max_execution_s",
        "ch_caller_shaped_max_memory_bytes",
        "vector_scan_max_concurrent_per_org",
        "vector_scan_max_waiters_per_org",
        "vector_scan_max_rows_to_read",
    )
    configuration = {name: getattr(settings, name) for name in names}
    assert configuration["ch_pool_maxsize"] == 4
    assert configuration["vector_scan_max_concurrent_per_org"] == 2
    query = (
        "SELECT sum(number) AS total, "
        "sum(cosineDistance([toFloat32(number + 1), 1.0], [1.0, 0.0])) AS score "
        "FROM numbers(4) WHERE sleepEachRow(0.4) = 0"
    )
    owned = []
    requests = []
    parent_reuse_id = "vector-process-parent-reuse-" + uuid.uuid4().hex
    requests.append(parent_reuse_id)
    root = (await asyncio.to_thread(Path(__file__).resolve)).parents[2]
    expected_core = hashlib.sha256(
        await asyncio.to_thread(Path(ch.__file__).read_bytes)
    ).hexdigest()

    async def start(label, barrier):
        request_id = "vector-process-" + uuid.uuid4().hex
        requests.extend((request_id, request_id + "-light"))
        payload = {
            "request_id": request_id,
            "settings": configuration,
            "query": query,
            "hot": state.hot,
            "light": state.light,
            "start_file": str(barrier),
        }
        log_path = tmp_path / (label + ".stderr")
        log = log_path.open("wb")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _VECTOR_RESTART_CHILD,
                json.dumps(payload),
                cwd=root,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
            )
        except BaseException:  # A failed/canceled spawn must close its private log descriptor.
            log.close()
            raise
        record = {
            "process": process,
            "log": log,
            "log_path": log_path,
            "request_id": request_id,
            "identity": None,
            "pidfd": None,
        }
        owned.append(record)  # Register before any identity/readiness failure can unwind.
        record["identity"] = _native_child_identity(process)
        record["pidfd"] = os.pidfd_open(process.pid)
        assert _native_child_identity(process) == record["identity"]
        ready = await _native_child_note(process, "ready")
        assert all(ready[key] == value for key, value in record["identity"].items())
        assert ready["settings"] == configuration and ready["core_sha256"] == expected_core
        assert ready["verified_tls"] == bool(settings.ch_secure)
        record["ready"] = ready
        return record

    async def complete(record):
        process = record["process"]
        light = await _native_child_note(process, "light")
        assert light["result"] == [{"score": 0.0, "complete": 7}] and light["hot_query_ids"]
        drained = await _native_child_note(process, "drained")
        assert drained["population"] == 0 and drained["orgs"] == {}
        assert drained["inflight"] == {} and drained["retired"] == {}
        assert drained["pool"]["in_use"] == 0 and drained["pool"]["waiters"] == 0
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        record["light"], record["drained"] = light, drained
        return drained["result"]

    try:
        barrier = tmp_path / "initial.start"
        first, peer = await start("first", barrier), await start("peer", barrier)
        assert first["identity"]["pid"] != peer["identity"]["pid"]
        assert first["ready"]["query_retries"] == peer["ready"]["query_retries"]
        barrier.touch()
        first_held = await _native_child_note(first["process"], "held")
        peer_held = await _native_child_note(peer["process"], "held")
        for record, held in ((first, first_held), (peer, peer_held)):
            assert held["held"] == held["population"] == 2
            assert held["pool_in_use"] >= 1 and len(held["query_ids"]) == 1
            active = await _vector_running(state.direct, record["request_id"])
            assert {row["query_id"] for row in active} == set(held["query_ids"])
        assert first_held["held"] + peer_held["held"] == 4  # Two local C2 gates, no fleet lock.
        immediately_before_stop = await _vector_running(state.direct, first["request_id"])
        assert {r["query_id"] for r in immediately_before_stop} == set(first_held["query_ids"])
        assert first["process"].returncode is None
        assert _native_child_identity(first["process"]) == first["identity"]
        signal.pidfd_send_signal(first["pidfd"], signal.SIGKILL)
        assert await asyncio.wait_for(first["process"].wait(), timeout=5) == -signal.SIGKILL
        after_reap = await _vector_running(state.direct, first["request_id"])
        # Restart promptly; waiting for old native retirement here would hide overlap.
        restart_barrier = tmp_path / "restart.start"
        replacement = await start("replacement", restart_barrier)
        assert replacement["ready"]["query_retries"] == peer["ready"]["query_retries"]
        restart_barrier.touch()
        restarted_held = await _native_child_note(replacement["process"], "held")
        assert restarted_held["held"] == restarted_held["population"] == 2
        peer_result = await complete(peer)
        restarted_result = await complete(replacement)
        assert restarted_result == peer_result and len(peer_result) == 1
        old_events = await _vector_remote_end(
            state.direct, first["request_id"], first_held["query_ids"]
        )
        new_events = await _vector_remote_end(
            state.direct, replacement["request_id"], restarted_held["query_ids"]
        )
        peer_events = await _vector_remote_end(
            state.direct, peer["request_id"], peer_held["query_ids"]
        )
        for record in (peer, replacement):
            await _vector_remote_end(state.direct, record["request_id"] + "-light", [])
        starts = [r for r in old_events if r["event"] == "QueryStart"]
        ends = [r for r in old_events if r["event"] != "QueryStart"]
        new_starts = [r for r in new_events if r["event"] == "QueryStart"]
        new_ends = [r for r in new_events if r["event"] != "QueryStart"]
        peer_starts = [r for r in peer_events if r["event"] == "QueryStart"]
        peer_ends = [r for r in peer_events if r["event"] != "QueryStart"]
        assert len(starts) == len(ends) == len(new_starts) == len(new_ends) == 1
        assert len(peer_starts) == len(peer_ends) == 1
        initial_overlap_us = min(ends[0]["at_us"], peer_ends[0]["at_us"]) - max(
            starts[0]["at_us"], peer_starts[0]["at_us"]
        )
        assert initial_overlap_us > 0, "two owned native clients did not actually overlap"
        overlap_us = max(
            0,
            min(ends[0]["at_us"], new_ends[0]["at_us"])
            - max(starts[0]["at_us"], new_starts[0]["at_us"]),
        )
        _vector_drained(state)
        assert await _vector_call(
            state.client,
            state.light,
            parent_reuse_id,
            "SELECT cosineDistance([1.0, 0.0], [1.0, 0.0]) AS score, 7 AS complete",
        ) == [{"score": 0.0, "complete": 7}]
        await _vector_remote_end(state.direct, parent_reuse_id, [])
        _vector_drained(state)
        print(
            "vector-native-process-restart "
            + json.dumps(
                {
                    "local_capacity_per_child": 2,
                    "simultaneous_local_held_sum": 4,
                    "first_identity": first["identity"],
                    "peer_identity": peer["identity"],
                    "replacement_identity": replacement["identity"],
                    "actual_core_sha256": expected_core,
                    "configuration": configuration,
                    "killed_child_returncode": first["process"].returncode,
                    "old_native_immediately_before_stop": immediately_before_stop,
                    "old_native_after_reap": after_reap,
                    "old_native_terminal": ends,
                    "replacement_native_start": new_starts,
                    "initial_two_process_native_overlap_us": initial_overlap_us,
                    "old_native_overlap_after_restart_us": overlap_us,
                    "peer_light": peer["light"],
                    "replacement_light": replacement["light"],
                    "complete_result": restarted_result,
                    "all_owned_processes_reaped": True,
                    "client_processes_are_not_web_workers": True,
                },
                sort_keys=True,
            )
        )
    finally:
        failure = sys.exception()
        cleanup_errors = []
        for record in reversed(owned):
            process, descriptor = record["process"], record["pidfd"]
            try:
                if process.returncode is None and descriptor is not None:
                    try:
                        assert _native_child_identity(process) == record["identity"]
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    except (ProcessLookupError, FileNotFoundError):
                        pass
                await asyncio.wait_for(process.wait(), timeout=5 if descriptor is not None else 20)
            except Exception as error:  # noqa: BLE001 — attempt every owned child/native cleanup
                cleanup_errors.append(type(error).__name__)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                record["log"].close()
                if failure is not None:
                    failure.add_note(redact(record["log_path"].read_text(errors="replace")[-4000:]))
        for request_id in requests:
            try:
                await _vector_cleanup(state.direct, request_id, failure)
            except Exception as error:  # noqa: BLE001 — remaining exact owned queries still need cleanup
                cleanup_errors.append(type(error).__name__)
        if cleanup_errors:
            if failure is not None:
                failure.add_note("own process cleanup: " + ",".join(cleanup_errors))
            else:
                raise AssertionError("own process cleanup: " + ",".join(cleanup_errors))
