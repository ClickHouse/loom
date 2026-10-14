"""Async ClickHouse client wrapper.

One global async client per process, created at startup and reused.
Parameterized queries only. Every query is wrapped in a one-shot
reconnect-retry so a server-side idle-connection close (CH Cloud trims
idle connections) doesn't surface as a hard failure.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import FrameType
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.external import ExternalData

from server.core.ch_class_settings import is_read_statement, traffic_class_settings
from server.core.ch_errors import (
    QUANTIZED_SETTING,
    REPLICA_BOUND_RETRIES,
    error_code,
    is_replica_bound_error,
    is_transient_network_error,
    is_unknown_quantized_setting,
    strip_quantized_setting,
)
from server.core.ch_request_cost import note_result_summary, request_query_id
from server.core.ch_schema_guard import guard_pending_schema
from server.core.ch_statement_text import capture_statement, statement_digest
from server.core.ch_target import connection_kwargs
from server.core.ch_writes import (
    dedup_settings,
    insert_dedup_token,
    normalize_insert_settings,
)
from server.core.traffic_context import traffic_class_var
from server.settings import settings

log = logging.getLogger(__name__)

# Every ClickHouse query names the operation that issued it.
# The transports set this to "<op> <request_id>" per request and the worker
# supervisor to "worker.<name>" per pass; the client passes it as the
# `log_comment` setting on every query/command/insert, so system.query_log
# joins loom's own telemetry by operation and by request. A setting, never
# query text: plans and the normalised query hash are unaffected.
query_tag_var: ContextVar[str] = ContextVar("ch_query_tag", default="")

# Called with (table, rows) after every successful `CHClient.insert`: the
# write marks and recent-write registries the domains keep. Registered by
# server/wiring.py; an observer that raises never fails the write.
_INSERT_OBSERVERS: list[Callable[[str, list[dict[str, Any]]], None]] = []


def add_insert_observer(fn: Callable[[str, list[dict[str, Any]]], None]) -> None:
    """Register `fn` once; a second registration of the same function is a no-op."""
    if fn not in _INSERT_OBSERVERS:
        _INSERT_OBSERVERS.append(fn)


_QUERY_TAG_MAX = 120


def set_query_tag(tag: str) -> None:
    """Attribute this task's ClickHouse queries to `tag` (empty clears)."""
    query_tag_var.set(str(tag or "")[:_QUERY_TAG_MAX])


def current_query_tag() -> str:
    return query_tag_var.get()


# The ClickHouse `quota_key` of the verified caller, `<org>:<subject>`, so a
# server quota keyed by it gives each tenant its own bucket. Set only from the
# verified token (`server.auth.context.quota_key_for`), never from a request
# body; every query of the request carries it, and so does the ledger work the
# MCP transport detaches for it (`mcp_runs`).
quota_key_var: ContextVar[str] = ContextVar("ch_quota_key", default="")


def set_quota_key(key: str) -> None:
    quota_key_var.set(str(key or ""))


# Reads a caller shapes (its regex, globs or aggregate over a namespace) run
# under fixed ceilings on time, memory and rows read. A ceiling only ever
# lowers the statement's own value, so neither a request nor a per-call setting
# widens it, and a row cap fails the read rather than truncating it.
caller_shaped_var: ContextVar[bool] = ContextVar("ch_caller_shaped", default=False)


@contextmanager
def caller_shaped() -> Iterator[None]:
    """Every read statement in this block runs under the caller-shaped ceilings."""
    token = caller_shaped_var.set(True)
    try:
        yield
    finally:
        caller_shaped_var.reset(token)


def _caller_shaped_ceilings() -> dict[str, float]:
    return {
        "max_execution_time": float(settings.ch_caller_shaped_max_execution_s),
        "max_memory_usage": float(settings.ch_caller_shaped_max_memory_bytes),
        "max_rows_to_read": float(settings.ch_caller_shaped_max_rows_to_read),
    }


def _apply_caller_ceilings(out: dict[str, Any], cls: str) -> None:
    """Lower each limit to its ceiling for a read-class statement inside a
    `caller_shaped()` block (0 in ClickHouse means unlimited, so an unset or 0
    value takes the ceiling). Every query-path statement is read-class, the
    same statements `_reader_auth` sends as the reader user."""
    if not caller_shaped_var.get() or cls != "read":
        return
    for name, ceiling in _caller_shaped_ceilings().items():
        if ceiling <= 0:
            continue
        asked = float(out.get(name) or 0)
        if asked <= 0 or asked > ceiling:
            out[name] = int(ceiling) if ceiling.is_integer() else ceiling
    out["read_overflow_mode"] = "throw"


def _reader_auth() -> dict[str, Any]:
    """Inside a `caller_shaped()` block with LOOM_CH_READER_USER set, the HTTP
    auth header that sends the read as that read-only user instead of `ch_user`.
    Only the query path sends it, where every statement is read-class and so
    carries the caller-shaped ceilings too."""
    user = settings.ch_reader_user
    if not caller_shaped_var.get() or not user:
        return {}
    token = base64.b64encode(f"{user}:{settings.ch_password}".encode()).decode()
    return {"transport_settings": {"Authorization": f"Basic {token}"}}


# Whether the boot probe could run a read as LOOM_CH_READER_USER: None = not
# probed (or no reader user set). False means every caller-shaped read fails,
# and `_reader_user_error` holds ClickHouse's answer.
_reader_user_ok: bool | None = None
_reader_user_error = ""

# How ClickHouse answers a read of loom.memory before migrations created it.
BEFORE_MIGRATIONS_CODES = frozenset({60, 81})  # UNKNOWN_TABLE, UNKNOWN_DATABASE

# Reads a loom table under the caller-shaped caps, so a missing grant or a cap
# above the reader profile's fails the probe as it would fail the reads.
READER_PROBE_SQL = (
    "SELECT currentUser() AS u, count() AS n FROM (SELECT 1 FROM loom.memory LIMIT 0)"
)


async def probe_reader_user(client: Any) -> None:
    """At boot, run one read as the reader user, so a ClickHouse whose users.d
    lacks it shows up in the problem list instead of as failing reads later."""
    global _reader_user_ok, _reader_user_error
    if not settings.ch_reader_user:
        return
    try:
        with caller_shaped():
            row = await client.query_one(READER_PROBE_SQL)
        _reader_user_ok = bool(row) and row["u"] == settings.ch_reader_user
        _reader_user_error = ""
    except Exception as e:  # noqa: BLE001 — a failed probe is reported, never fatal to boot
        if error_code(e) in BEFORE_MIGRATIONS_CODES:
            return  # no loom.memory yet: migrations have not run, nothing to probe
        log.warning("LOOM_CH_READER_USER=%s cannot read: %s", settings.ch_reader_user, e)
        _reader_user_ok = False
        _reader_user_error = str(e)


def reader_user_state() -> bool | None:
    return _reader_user_ok


def reader_user_error() -> str:
    return _reader_user_error


# Caps a caller-shaped read hit: TOO_MANY_ROWS, TOO_MANY_BYTES, TIMEOUT_EXCEEDED.
_CALLER_CAP_CODES = (158, 307, 159)
_MEMORY_LIMIT_EXCEEDED = 241


def _is_caller_cap(e: BaseException) -> bool:
    code = error_code(e)
    if code == _MEMORY_LIMIT_EXCEEDED:
        # Only the per-query limit is this request's breadth; the server total
        # or the user's limit is the server or the shared user running short.
        text = str(e)
        return "Query memory limit exceeded" in text or "(for query)" in text
    return code in _CALLER_CAP_CODES


def _raise_if_caller_cap(e: BaseException) -> None:
    """A caller-shaped read that hit its row, time or memory cap is the request's
    breadth, not a server fault: a 422 that says how to narrow it."""
    if caller_shaped_var.get() and _is_caller_cap(e):
        from server.core.errors import ValidationError

        raise ValidationError(
            "the query needs more rows, time or memory than this deployment allows one "
            "request; narrow it (a path prefix or globs, a more selective pattern, a filter)"
        ) from None


# Read-your-writes for whole read paths. The point lookups append `SETTINGS
# select_sequential_consistency = 1` to one statement; a search runs a dozen statements
# across legs and namespaces, so the retry is expressed as a scope instead: every query
# issued while the scope is active carries the setting. Tasks spawned inside the scope
# inherit it (contextvars copy on task creation); nothing leaks out.
sequential_reads_var: ContextVar[bool] = ContextVar("ch_sequential_reads", default=False)


@contextmanager
def sequential_reads() -> Iterator[None]:
    """Every CH query in this block reads with sequential consistency."""
    token = sequential_reads_var.set(True)
    try:
        yield
    finally:
        sequential_reads_var.reset(token)


def sequential_reads_active() -> bool:
    return bool(sequential_reads_var.get())


@contextmanager
def own_write_reads() -> Iterator[None]:
    """A point read a caller makes right after its own write
    — working memory, a knowledge document, a memory by key, a profile
    section — reads with sequential consistency. The server runs several web
    workers over a pool spread across Cloud replicas, so the next call of a
    session rarely lands where its write did (most get-after-set calls served
    the old value without it). On these small point reads the Keeper round-trip costs
    ~10 ms, so the scope is unconditional rather than a per-process window
    that a request on another worker would never see."""
    if sequential_reads_var.get():
        yield
        return
    token = sequential_reads_var.set(True)
    try:
        yield
    finally:
        sequential_reads_var.reset(token)


# Every statement belongs to a class with its own deadline.
# `read` is a SELECT-shaped statement (30 s), `write` an INSERT or a
# lightweight UPDATE (60 s), `ddl` a mutation, table DDL, OPTIMIZE or SYSTEM
# statement (300 s). The bound is applied twice per call: as
# `max_execution_time`, so the server stops the work, and as a client-side
# `asyncio.timeout` on the same number, so the pool slot is released even
# when the server never answers. The driver's own read timeout stays at the
# largest class as the outer backstop.
_DDL_LEADS = (
    "ALTER",
    "CREATE",
    "DROP",
    "TRUNCATE",
    "OPTIMIZE",
    "SYSTEM",
    "KILL",
    "RENAME",
    "ATTACH",
    "DETACH",
    "EXCHANGE",
    "DELETE",  # DELETE FROM is a lightweight mutation
)
_WRITE_LEADS = ("INSERT", "UPDATE")
_LEAD_RE = re.compile(r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*([A-Za-z]+)", re.S)

# A block that knows it runs long (a CLI mutation that waits on
# `mutations_sync = 2`, a backfill) raises the bound for every statement it
# issues, without touching the class defaults. Tasks spawned inside inherit it.
statement_timeout_var: ContextVar[float] = ContextVar("ch_statement_timeout", default=0.0)


@contextmanager
def statement_timeout(seconds: float) -> Iterator[None]:
    """Every CH statement in this block gets `seconds` as its deadline."""
    token = statement_timeout_var.set(float(seconds))
    try:
        yield
    finally:
        statement_timeout_var.reset(token)


def _leading_keyword(sql: str) -> str:
    m = _LEAD_RE.match(sql or "")
    return (m.group(1) if m else "").upper()


def statement_class(sql: str) -> str:
    """`read`, `write` or `ddl` from the statement's leading keyword."""
    lead = _leading_keyword(sql)
    if lead in _WRITE_LEADS:
        return "write"
    if lead in _DDL_LEADS:
        return "ddl"
    return "read"


def class_timeout_s(cls: str) -> float:
    """The deadline for `cls` in seconds; 0 means unbounded. A
    `statement_timeout()` scope wins over the class default."""
    scoped = statement_timeout_var.get()
    if scoped > 0:
        return scoped
    if cls == "write":
        return float(settings.ch_timeout_write_s)
    if cls == "ddl":
        return float(settings.ch_timeout_ddl_s)
    return float(settings.ch_timeout_read_s)


_DRIVER_DEFAULT_READ_TIMEOUT_S = 300


def _driver_read_timeout_s() -> int:
    """The driver-level read timeout: the `ddl` bound, or the driver's own
    default when every class bound is off (0 must never mean 1 s)."""
    t = class_timeout_s("ddl")
    return int(t) if t > 0 else _DRIVER_DEFAULT_READ_TIMEOUT_S


def _apply_class_timeout(out: dict[str, Any], cls: str) -> dict[str, Any]:
    t = class_timeout_s(cls)
    if t > 0 and "max_execution_time" not in out:
        out["max_execution_time"] = int(t) if float(t).is_integer() else t
    return out


# The slot gate. One asyncio.Semaphore the size of the
# connection pool per process; a statement that cannot get a slot inside
# `ch_pool_wait_ms` fails the request with 503 OVERLOADED instead of queueing
# in the HTTP connector for the length of a read timeout. The counts below
# feed the `loom.ch.pool.*` gauges.
_POOL_WAITERS = 0
_POOL_IN_USE = 0
_POOL_OVERLOADED_TOTAL = 0
_POOL_TIMEOUTS_TOTAL = 0
_OVERLOADED_LOG_EVERY_S = 5.0
_overloaded_last_log = 0.0
_overloaded_since_log = 0


def pool_reading() -> dict[str, int]:
    """`{limit, in_use, waiters, overloaded_total, timeouts_total}` for this
    process — a snapshot, no store touched."""
    return {
        "limit": int(settings.ch_pool_maxsize),
        "in_use": _POOL_IN_USE,
        "waiters": _POOL_WAITERS,
        "overloaded_total": _POOL_OVERLOADED_TOTAL,
        "timeouts_total": _POOL_TIMEOUTS_TOTAL,
    }


def _with_request_query_id(out: dict[str, Any]) -> dict[str, Any]:
    if "query_id" not in out:
        qid = request_query_id(query_tag_var.get())
        if qid:
            out["query_id"] = qid
    return out


def statement_settings(
    sql: str, extra: dict[str, Any] | None = None, cls: str | None = None
) -> dict[str, Any]:
    """`query_settings()` for one statement plus the class deadline as
    `max_execution_time` (the caller's own value wins); with
    LOOM_CH_LOG_COMMENT_STATEMENT_DIGEST the digest follows the tag in
    `log_comment` (or stands alone when no tag is set), so system.query_log
    can be grouped per statement shape without reading the `query` text.

    A read statement (SELECT / WITH) issued under a traffic
    class also carries that class's settings from
    LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS (`priority`, `max_threads`, …) — the
    caller's own `extra` wins on a name it already sets, and neither setting
    changes which rows the statement returns."""
    out = query_settings(extra)
    cls = cls or statement_class(sql)
    _apply_class_timeout(out, cls)
    if os.environ.get("LOOM_CH_CAPTURE_STATEMENTS"):
        capture_statement(sql)
    if settings.ch_log_comment_statement_digest:
        tag = str(out.get("log_comment") or "")
        out["log_comment"] = f"{tag} {statement_digest(sql)}".strip()
    tclass = traffic_class_var.get()[0]
    if tclass and is_read_statement(sql):
        for name, value in traffic_class_settings(tclass).items():
            out.setdefault(name, value)
    _apply_caller_ceilings(out, cls)
    return _with_request_query_id(out)


def _untagged_caller() -> str:
    """`untagged:<module>.<function>` for the nearest server frame above the
    ClickHouse plumbing, or '' when the call comes from outside `server`.

    A query issued outside a request or a worker pass (a boot loop, a gauge
    collector, work handed to a thread) has no tag, and such queries can be a
    large share of read bytes. Naming the caller keeps the query-tag join to
    `system.query_log` working for them. Runs only for
    untagged queries: a frame walk is microseconds against a network trip."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("server.") and not _is_plumbing(module):
            where = f"{module.removeprefix('server.')}.{frame.f_code.co_name}"
            return f"untagged:{where}"[:_QUERY_TAG_MAX]
        frame = frame.f_back
    return ""


# Frames that only carry a query to ClickHouse; the caller worth naming is above them:
# the client, the storage adapters, and every domain package's store layer.
_PLUMBING_MODULES = ("server.core.ch", "server.storage.")


def _is_plumbing(module: str) -> bool:
    return module.startswith(_PLUMBING_MODULES) or ".store." in module


def query_settings(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-query settings: the caller's `extra` plus `log_comment` — the tag,
    or the calling function when no tag is set — and
    `select_sequential_consistency` inside a `sequential_reads()` scope."""
    out: dict[str, Any] = dict(extra or {})
    if "log_comment" not in out:
        tag = query_tag_var.get() or _untagged_caller()
        if tag:
            out["log_comment"] = tag
    if sequential_reads_var.get() and "select_sequential_consistency" not in out:
        out["select_sequential_consistency"] = 1
    # the verified caller's key always wins over anything a call passed
    key = quota_key_var.get()
    if key:
        out["quota_key"] = key
    return out


async def _probe_vector_state(client: CHClient) -> None:
    """The vector read state (does every replica know the codes setting, which
    columns are coded, which tables carry patch parts) is probed by whichever
    process connects first, not only by the server's lifespan, so CLIs and
    benches build the same reads as the server. Once per process; a reconnect
    skips it. Unit tests (fake clients) replace this function."""
    from server.core.vectors.codes_state import ensure_probed

    await ensure_probed(client)


class CHClient:
    """Singleton-ish wrapper. Use `get_client()` to fetch."""

    # Reconnect and slot-gate state; created lazily in `_ensure_state` (see there for
    # why), declared here so the type checker knows the shape.
    _generation: int
    _inflight: dict[int, int]
    _retired: dict[int, AsyncClient]
    _slots: asyncio.Semaphore | None
    _reconnect_lock: asyncio.Lock

    def __init__(self) -> None:
        self._client: AsyncClient | None = None
        self._reconnect_lock = asyncio.Lock()
        self._ensure_state()

    def _ensure_state(self) -> None:
        """Reconnect and slot-gate state, created on first use so a client built with
        `__new__` (the unit-test idiom) has it too: the generation counter
        that makes a reconnect a no-op for a task whose failure another task
        already fixed, the in-flight count per generation that lets a
        replaced client close only once its last statement has finished, and
        the slot gate the size of the pool."""
        d = self.__dict__
        d.setdefault("_generation", 0)
        d.setdefault("_inflight", {})
        d.setdefault("_retired", {})
        if "_slots" not in d:
            limit = int(settings.ch_pool_maxsize)
            d["_slots"] = asyncio.Semaphore(limit) if limit > 0 else None
        if "_reconnect_lock" not in d:
            d["_reconnect_lock"] = asyncio.Lock()

    async def connect(self) -> None:
        self._client = await clickhouse_connect.get_async_client(
            **connection_kwargs(),
            compress=True,
            # The driver's read timeout is the outer backstop
            # at the largest class bound (the driver's own 300 s when that
            # bound is disabled); each statement's own deadline is applied
            # per call in `_with_retry`.
            send_receive_timeout=_driver_read_timeout_s(),
            connector_limit=settings.ch_pool_maxsize,
            connector_limit_per_host=settings.ch_pool_maxsize,
            # Close idle pooled connections before the Cloud load balancer
            # does, so the pool never holds a peer-closed TLS transport.
            keepalive_timeout=float(settings.ch_keepalive_timeout),
            # Writes are committed: the INSERT returns after the part is
            # written (at loom's write rates the async buffer does not batch and
            # only adds queueing). Block dedup stays off: identical event rows
            # (agent_events, audit_log) are legitimate and must all land.
            settings={
                "async_insert": 0,
                "wait_for_async_insert": 1,
                "insert_deduplicate": 0,
                # Vector search scans the embedding column; on ClickHouse
                # Cloud that reads object storage unless the local filesystem
                # cache serves it, and a service profile can ship with the
                # cache off. Session-level, so every query benefits; a no-op
                # on a server without a cache.
                "enable_filesystem_cache": 1,
            },
        )
        await _probe_vector_state(self)

    async def disconnect(self) -> None:
        self._ensure_state()
        retired = list(self._retired.values())
        self._retired.clear()
        for old in retired:
            try:
                await old.close()
            except Exception:  # noqa: BLE001 — closing is best effort
                pass
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("ClickHouse client not connected; call connect() first")
        return self._client

    @property
    def generation(self) -> int:
        self._ensure_state()
        return int(self._generation)

    async def _reconnect(self, seen: int | None = None) -> None:
        """Replace the shared client with a fresh one.

        `seen` is the generation the caller's failed statement ran on. Under
        the lock, a caller whose generation is already behind returns at
        once and retries on the client another task built — N tasks that
        fail together cost one reconnect, not N. The new client is built
        BEFORE the old one is retired, so no task ever observes "not
        connected"; the old client closes when its last in-flight statement
        finishes (or now, when none is), never under a task still using it.
        A failed connect leaves the old client in place and raises."""
        self._ensure_state()
        async with self._reconnect_lock:
            if seen is not None and self._generation != seen:
                return
            old = self._client
            old_gen = self._generation
            await self.connect()  # assigns self._client on success only
            self._generation = old_gen + 1
            if old is not None:
                if self._inflight.get(old_gen, 0) > 0:
                    self._retired[old_gen] = old
                else:
                    try:
                        await old.close()
                    except Exception:  # noqa: BLE001 — closing is best effort
                        pass
            log.info("ch: reconnected after transient failure (generation %d)", self._generation)

    async def _retire_if_done(self, gen: int) -> None:
        if self._inflight.get(gen, 0) > 0:
            return
        self._inflight.pop(gen, None)
        old = self._retired.pop(gen, None)
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001 — closing is best effort
                pass

    async def _bounded(self, op_name: str, cls: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run `fn` once under the class deadline, counted against the
        generation it started on."""
        global _POOL_TIMEOUTS_TOTAL
        gen = int(self._generation)
        self._inflight[gen] = self._inflight.get(gen, 0) + 1
        limit = class_timeout_s(cls)
        try:
            if limit <= 0:
                return await fn()
            try:
                async with asyncio.timeout(limit) as cm:
                    return await fn()
            except TimeoutError:
                if not cm.expired():
                    raise
                _POOL_TIMEOUTS_TOTAL += 1
                from server.core.errors import BackendTimeoutError

                log.warning(
                    "ch %s: %s statement ran past its %.0f s deadline; slot released, not retried",
                    op_name,
                    cls,
                    limit,
                )
                raise BackendTimeoutError(
                    f"ClickHouse {cls} statement exceeded {limit:g} s",
                    statement_class=cls,
                    timeout_s=limit,
                ) from None
        finally:
            self._inflight[gen] = self._inflight.get(gen, 1) - 1
            await self._retire_if_done(gen)

    async def _acquire_slot(self, op_name: str) -> bool:
        """Take one pool slot or raise 503 OVERLOADED after `ch_pool_wait_ms`.
        Returns whether a slot was taken (False when the gate is off)."""
        global _POOL_WAITERS, _POOL_IN_USE, _POOL_OVERLOADED_TOTAL
        global _overloaded_last_log, _overloaded_since_log
        sem = self._slots
        wait_ms = int(settings.ch_pool_wait_ms)
        if sem is None or wait_ms <= 0:
            return False
        if sem.locked():
            _POOL_WAITERS += 1
            try:
                await asyncio.wait_for(sem.acquire(), timeout=wait_ms / 1000.0)
            except TimeoutError:
                _POOL_OVERLOADED_TOTAL += 1
                _overloaded_since_log += 1
                now = time.monotonic()
                if now - _overloaded_last_log >= _OVERLOADED_LOG_EVERY_S:
                    log.warning(
                        "ch %s: no pool slot within %d ms (%d refused since last line; "
                        "limit %d, in use %d, waiting %d) — 503 OVERLOADED",
                        op_name,
                        wait_ms,
                        _overloaded_since_log,
                        int(settings.ch_pool_maxsize),
                        _POOL_IN_USE,
                        _POOL_WAITERS - 1,
                    )
                    _overloaded_last_log = now
                    _overloaded_since_log = 0
                from server.core.errors import OverloadedError

                raise OverloadedError(
                    "every ClickHouse connection of this process is busy; retry shortly",
                    retry_after_s=1,
                ) from None
            finally:
                _POOL_WAITERS -= 1
        else:
            await sem.acquire()
        _POOL_IN_USE += 1
        return True

    def _release_slot(self) -> None:
        global _POOL_IN_USE
        _POOL_IN_USE -= 1
        if self._slots is not None:
            self._slots.release()

    async def _with_retry(  # type: ignore[no-untyped-def]
        self,
        op_name: str,
        fn,
        *,
        cls: str = "read",
    ):
        """Run `fn` under the slot gate and the class deadline, with the
        retry ladder: a transient network failure is retried exactly once on
        a fresh connection (every INSERT carries a deduplication token, so a
        retried batch lands once); a replica-bound server error gets up to two
        more connections; a class deadline and every other error raise at once."""
        self._ensure_state()
        took = await self._acquire_slot(op_name)
        try:
            return await self._retry_ladder(op_name, fn, cls=cls)
        finally:
            if took:
                self._release_slot()

    async def _retry_ladder(self, op_name: str, fn, *, cls: str):  # type: ignore[no-untyped-def]
        seen = int(self._generation)
        try:
            return await self._bounded(op_name, cls, fn)
        except Exception as e:
            if is_transient_network_error(e):
                log.warning(
                    "ch %s: transient %s, reconnecting and retrying",
                    op_name,
                    type(e).__name__,
                )
                try:
                    await self._reconnect(seen)
                    return await self._bounded(op_name, cls, fn)
                except Exception as retry_error:
                    if not is_transient_network_error(retry_error):
                        raise
                    from server.core.errors import BackendUnavailableError

                    raise BackendUnavailableError(
                        "ClickHouse is unavailable; check /v1/health and the server logs, "
                        "then restart it with `loom up`.",
                        operation=op_name,
                    ) from retry_error
            _raise_if_caller_cap(e)
            if not is_replica_bound_error(e):
                raise
            last: BaseException = e
            for attempt in range(1, REPLICA_BOUND_RETRIES + 1):
                log.warning(
                    "ch %s: replica-bound server error (%s), retry %d/%d on a new connection",
                    op_name,
                    str(last)[:80],
                    attempt,
                    REPLICA_BOUND_RETRIES,
                )
                await self._reconnect(int(self._generation))
                try:
                    return await self._bounded(op_name, cls, fn)
                except Exception as e2:
                    if not is_replica_bound_error(e2):
                        raise
                    last = e2
            raise last from None

    async def query_one(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Execute a SELECT and return the first row as a dict, or None if no rows."""

        result = await self._query_fail_open("query_one", sql, params)
        rows = result.result_rows
        if not rows:
            return None
        return dict(zip(result.column_names, rows[0], strict=False))

    async def _query_fail_open(self, op_name: str, sql: str, params: dict[str, Any] | None) -> Any:
        """Run a SELECT with the standard retry; if the server rejects the
        quantized-codes setting (a replica that does not know it), turn the
        setting off for this process and run the query once
        more without the fragment. The codec path is a speed-up; a read that
        fails for want of it is the wrong trade."""

        async def _q(text: str) -> Any:
            res = await self.client.query(
                text,
                parameters=params or {},
                settings=statement_settings(text, cls="read"),
                **_reader_auth(),
            )
            note_result_summary(res)
            return res

        await guard_pending_schema(op_name, sql, "read")
        try:
            return await self._with_retry(op_name, lambda: _q(sql), cls="read")
        except Exception as e:
            if not is_unknown_quantized_setting(e):
                raise
            stripped = strip_quantized_setting(sql)
            from server.core.vectors import quantized_setting

            if quantized_setting.quantized_codes_supported():
                log.warning(
                    "ch %s: a replica rejected %s (%s); turning the setting off for "
                    "this process and retrying without it",
                    op_name,
                    QUANTIZED_SETTING,
                    str(e)[:80],
                )
                quantized_setting.set_quantized_codes_supported(False)
            if stripped == sql:
                raise
            return await self._with_retry(op_name, lambda: _q(stripped), cls="read")

    async def query_all(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._query_fail_open("query_all", sql, params)
        return [dict(zip(result.column_names, row, strict=False)) for row in result.result_rows]

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        settings: dict[str, Any] | None = None,
        external_data: ExternalData | None = None,
    ) -> None:
        """Execute a non-returning statement (INSERT, ALTER, DDL). The class
        deadline follows the leading keyword. A raw INSERT
        gets a deduplication token from its text and parameters, so the
        driver's retry after a peer close cannot land it twice. `settings` are
        this statement's own ClickHouse settings. Large typed batches can use
        `external_data` instead of oversized HTTP parameter fields."""
        cls = statement_class(sql)
        extra: dict[str, Any] | None = dict(settings) if settings else None
        if cls == "write" and _leading_keyword(sql) == "INSERT":
            token = insert_dedup_token(
                "sql", [sql], [{sql: sorted((params or {}).items())}], uuid.uuid4().hex
            )
            extra = dedup_settings(token, extra)

        async def _q() -> None:
            payload: dict[str, Any] = (
                {"external_data": external_data} if external_data is not None else {}
            )
            res = await self.client.command(
                sql,
                parameters=params or {},
                settings=statement_settings(sql, extra, cls=cls),
                **payload,
            )
            note_result_summary(res)

        await guard_pending_schema("execute", sql, cls)
        await self._with_retry("execute", _q, cls=cls)

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
        *,
        project_onto_table: bool | None = None,
    ) -> None:
        """Batch insert via the native typed insert path. `settings` forwards
        CH query settings; fire-and-forget settings are normalised by
        `normalize_insert_settings`.

        `project_onto_table` (default on for the ledger tables
        in `PROJECTED_TABLES`): columns the live table does not have are
        dropped from the statement — one WARNING per (table, column) per
        process — instead of failing the batch. A process running code newer
        than the service's schema (an isolated server, or the deployment
        between an image build and its boot migration) then loses a column's
        values, not its rows."""
        if not rows:
            return
        # Before the DESCRIBE — that read is the first statement
        # to raise code 60 against a table the service does not have yet.
        await guard_pending_schema("insert", "", "write", insert_table=table)
        # A coded vector column needs every row at full length.
        from server.core.vectors.placeholders import fit_vectors

        await fit_vectors(table, rows)
        columns = list(rows[0].keys())
        if project_onto_table is None:
            project_onto_table = table in PROJECTED_TABLES
        if project_onto_table:
            columns = await self._project_columns(table, columns)
        # Rows must agree on their key set: the column list comes from row 0,
        # so a row missing a key would raise a bare KeyError deep in the
        # driver (and a row with an extra key would silently drop it). Name
        # the offending row and columns instead.
        want = set(rows[0].keys())
        for i, r in enumerate(rows):
            have = set(r.keys())
            if have != want:
                missing = sorted(want - have)
                extra = sorted(have - want)
                raise ValueError(
                    f"insert {table}: row {i} keys differ from row 0 — "
                    f"missing {missing}, extra {extra}"
                )
        data = [[r[c] for c in columns] for r in rows]

        # The driver asks the server for the column types of
        # every insert it is not told them for — one `DESCRIBE TABLE` round
        # trip per insert (thousands an hour for the ledgers, each a pooled
        # connection held for a trip that returns nothing new). The
        # types are read once per table per process and handed to the driver;
        # a schema that has changed under the cache fails the insert once,
        # the entry is dropped, and the insert runs again the old way.
        type_names = await self._column_type_names(table, columns)

        settings = normalize_insert_settings(settings)
        # One token per batch, drawn once here, so every
        # attempt below — loom's retry, the re-describe retries and the
        # driver's own retry after a peer close — names the same batch
        # and ClickHouse keeps one copy.
        settings = dedup_settings(
            insert_dedup_token(table, list(rows[0].keys()), rows, uuid.uuid4().hex), settings
        )

        async def _q() -> None:
            res = await self.client.insert(
                table,
                data,
                column_names=columns,
                column_type_names=type_names,
                settings=_with_request_query_id(
                    _apply_class_timeout(query_settings(settings), "write")
                ),
            )
            note_result_summary(res)

        async def _run() -> None:
            await self._with_retry("insert", _q, cls="write")

        try:
            await _run()
        except Exception as e:
            from server.core.errors import LoomServerError

            # The re-describe ladder below re-runs the INSERT; never for a
            # refused slot or a cut deadline.
            if isinstance(e, LoomServerError):
                raise
            had_cache = type_names is not None or table in _COLUMN_TYPES
            _COLUMN_TYPES.pop(table, None)
            if project_onto_table:
                # The cached column set may be stale (a migration ran, or the
                # first describe failed): re-describe, re-project, and retry
                # once with what the table has NOW.
                reprojected = await self._project_columns(table, list(rows[0].keys()))
                if reprojected != columns:
                    columns = reprojected
                    data = [[r[c] for c in columns] for r in rows]
                    type_names = await self._column_type_names(table, columns)
                    await _run()
                    return
            if not had_cache:
                raise
            type_names = None
            await _run()
        # The write marks of the five plane tables are kept in-process and
        # flushed every few seconds; then the domains' observers.
        try:
            from server.core import namespace_write_marks as _marks

            _marks.observe_insert(table, rows)
        except Exception:  # noqa: BLE001 — bookkeeping never fails a write
            pass
        for observe in _INSERT_OBSERVERS:
            try:
                observe(table, rows)
            except Exception:
                log.debug("ch insert observer %r failed", observe, exc_info=True)

    async def _live_column_types(self, table: str) -> dict[str, str] | None:
        """`{column: type}` of `table` from the per-process cache, filled by
        one `DESCRIBE TABLE`. None when the describe fails."""
        types = _COLUMN_TYPES.get(table)
        if types is not None:
            return types
        try:
            sql = f"DESCRIBE TABLE {table}"

            async def _d() -> Any:
                res = await self.client.query(sql, settings=statement_settings(sql, cls="read"))
                note_result_summary(res)
                return res

            res = await self._with_retry("describe", _d, cls="read")
            names = list(res.column_names)
            i_name, i_type = names.index("name"), names.index("type")
            types = {str(r[i_name]): str(r[i_type]) for r in res.result_rows}
        except Exception as e:  # noqa: BLE001 — the cache is an optimisation
            log.debug("ch insert: describe %s failed, driver will (%s)", table, e)
            return None
        _COLUMN_TYPES[table] = types
        return types

    async def _column_type_names(self, table: str, columns: list[str]) -> list[str] | None:
        """Column type names for `columns` of `table`, from a per-process
        cache filled by one `DESCRIBE TABLE`. None (driver asks the server
        itself) when the describe fails or a column is unknown to it."""
        types = await self._live_column_types(table)
        if types is None:
            return None
        try:
            return [types[c] for c in columns]
        except KeyError:
            return None

    async def _project_columns(self, table: str, columns: list[str]) -> list[str]:
        """`columns` minus those the live `table` lacks. One
        WARNING per (table, column) per process — the same column is missing
        on every flush until the migration runs. Unchanged when the describe
        fails: the driver then reports the real error."""
        types = await self._live_column_types(table)
        if types is None:
            return columns
        kept = [c for c in columns if c in types]
        for c in columns:
            if c in types or (table, c) in _PROJECTION_WARNED:
                continue
            _PROJECTION_WARNED.add((table, c))
            log.warning(
                "ch insert: %s has no column %r on this service (schema behind this "
                "build?) — dropping it from every %s insert until the migration runs",
                table,
                c,
                table,
            )
        return kept


# Per-process column types by table, for inserts. Bounded by
# the number of tables loom writes; dropped per table on an insert failure.
_COLUMN_TYPES: dict[str, dict[str, str]] = {}

# Ledger tables whose inserts are projected onto the live column set by
# default: rows here are telemetry a request or a pass has
# already finished — losing a new column's value is a gap, losing the row is
# a hole in the cost and audit series.
# loom.llm_response_cache joins them: a cache row a service behind the schema
# cannot store whole is still a row worth storing.
PROJECTED_TABLES: frozenset[str] = frozenset(
    {
        "loom.agent_events",
        "loom.audit_log",
        "loom.events",
        "loom.llm_response_cache",
        # the credit's `consumer` column; a
        # build ahead of the service's schema keeps writing the credit.
        "loom.memory_usage_events",
    }
)
# (table, column) pairs already warned about — one line per process.
_PROJECTION_WARNED: set[tuple[str, str]] = set()


def describe_cache_clear() -> None:
    """Forget every cached column type. A test hook: nothing in the server
    calls it, so a column added under a running process reaches that table's
    projected inserts only after a restart or a failed insert drops the
    entry."""
    _COLUMN_TYPES.clear()
    _PROJECTION_WARNED.clear()


_ch = CHClient()


def get_client() -> CHClient:
    return _ch
