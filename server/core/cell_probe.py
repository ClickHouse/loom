"""The probe route: what a deployment answers so `loom cell prepare` and attach can tell
it is the one whose stores they write to (docs/architecture/cells.md, "Preparing a cell").

- On the main port, `GET /v1/health/deployment` answers only the deployment id this
  process verified at boot, held in memory: it reads no store, needs no credential and
  is not rate-limited, so a flood on the main port cannot stall attach's host check.
- On the probe listener (`LOOM_CELL_PROBE_PORT`, off by default) the same path also
  answers the values under prepare's nonce keys, read on that request from this
  process's own connections: the Postgres row `loom.cell_probe`, the ClickHouse key
  `loom._migration_lock` and the bucket object `<prefix>/_cell_probe`. None of these values
  is secret. It answers 429 past PROBES_PER_MINUTE per process. Every web process of a host
  accepts on the port (SO_REUSEPORT); the workers child opens none.

Prepare writes a nonce to each store, reads it back here, and deletes it. A copy of
another deployment's stores answers that deployment's id with none of this nonce.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import socket
import time
from collections.abc import Callable, Generator
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from server.core import cell
from server.settings import settings

log = logging.getLogger("loom.cell_probe")

PATH = "/v1/health/deployment"
# The name of the nonce in each store: the Postgres row, the KeeperMap key, the object.
NONCE_NAME = "cell-probe"
OBJECT_NAME = "_cell_probe"
PROBES_PER_MINUTE = 60  # not a setting: prepare and attach ask a few times, a flood more


def answer() -> dict[str, Any]:
    """The main port's answer: the id this process verified at boot, or '' before."""
    return {"deployment_id": cell.deployment_id()}


class StoreUnreadable(Exception):
    """A store the listener could not read, by name (`postgres`, `clickhouse`, `bucket`)."""

    def __init__(self, store: str, error: BaseException) -> None:
        # Words the listener writes: the store and the error's type, never its text.
        self.reason = f"{store} could not be read ({type(error).__name__})"
        super().__init__(self.reason)
        self.store = store


async def nonces() -> dict[str, str | None]:
    """The value under the nonce key in each store, None where there is none (or no
    bucket); a store that fails raises StoreUnreadable, naming it."""
    from server.core.ch import query_tag

    values: dict[str, str | None] = {}
    with query_tag("cell.probe"):
        for store, read in (
            ("postgres", _postgres),
            ("clickhouse", _clickhouse),
            ("bucket", _bucket),
        ):
            try:
                values[store] = await read()
            except Exception as e:  # re-raised with the store named, answered as a 503
                log.warning("probe: the %s nonce could not be read: %s", store, type(e).__name__)
                raise StoreUnreadable(store, e) from e
    return values


async def _postgres() -> str | None:
    from server.core import pg

    value = await pg.fetchval("SELECT nonce FROM loom.cell_probe WHERE name = $1", NONCE_NAME)
    return None if value is None else str(value)


async def _clickhouse() -> str | None:
    from server.core.ch import get_client
    from server.core.leases import LOCK_TABLE

    row = await get_client().query_one(
        f"SELECT holder FROM {LOCK_TABLE} WHERE name = {{name:String}}", {"name": NONCE_NAME}
    )
    return None if row is None else str(row["holder"])


async def _bucket() -> str | None:
    if not settings.document_s3_bucket:
        return None
    from server.core import object_store

    loc = object_store.Location(
        settings.document_s3_bucket, object_store.root_prefix() + OBJECT_NAME
    )
    data = await object_store.read(loc)
    return None if data is None else data.decode("utf-8", "replace")


class _Rate:
    """At most `per_minute` answers in any minute."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self.stamps: list[float] = []

    def wait_s(self) -> int:
        """0 when admitted (and counted), else the seconds until the next answer."""
        now = self.clock()
        self.stamps = [t for t in self.stamps if now - t < 60]
        if len(self.stamps) < self.per_minute:
            self.stamps.append(now)
            return 0
        return int(60 - (now - self.stamps[0])) + 1


def listener_app(rate: _Rate | None = None) -> Starlette:
    """The probe listener's app: the deployment id and the nonces, rate-limited."""
    limit = rate or _Rate(PROBES_PER_MINUTE)

    async def probe(_request: Request) -> JSONResponse:
        if wait := limit.wait_s():
            return JSONResponse(
                {"error": "too many probes"}, status_code=429, headers={"Retry-After": str(wait)}
            )
        try:
            values = await nonces()
        except StoreUnreadable as e:
            return JSONResponse(
                {"error": e.reason, "store": e.store},
                status_code=503,
            )
        return JSONResponse({**answer(), "nonces": values}, headers={"Cache-Control": "no-store"})

    return Starlette(routes=[Route(PATH, probe, methods=["GET"])])


class _QuietServer(uvicorn.Server):
    """uvicorn's server without its signal handlers, which belong to the main server."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def bind(port: int) -> socket.socket:
    """The listener's socket on every address: `::` (taking IPv4 too) where the host has
    IPv6, else `0.0.0.0`; who may reach the port is the network policy's, as the cells
    page says. SO_REUSEPORT shares it with the host's other web processes, so each takes
    its share of the probes. A port another program holds without SO_REUSEPORT raises at
    once; a process of the same user that sets it shares the port."""
    error: OSError | None = None
    for family, host in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError as e:
            error = e
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind((host, port))
        except OSError as e:
            sock.close()
            if e.errno == errno.EADDRINUSE:
                raise
            error = e
            continue
        return sock
    assert error is not None
    raise error


class Listener:
    """The running probe listener: `stop()` asks uvicorn to shut down and waits for it,
    so its servers close their sockets themselves."""

    STOP_S = 5.0  # not a setting: a listener with no request in flight stops at once

    def __init__(self, sock: socket.socket) -> None:
        # No log_config: the process's logging, the main server's loggers included,
        # stays as it is.
        config = uvicorn.Config(listener_app(), log_config=None, lifespan="off")
        self.server = _QuietServer(config)
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]), name="cell-probe")

    async def stop(self) -> None:
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, self.STOP_S)  # cancels it past the bound
        except Exception as e:  # noqa: BLE001 — a listener that will not stop must not hold up shutdown
            log.warning("the probe listener did not stop cleanly: %s", type(e).__name__)


def start_listener() -> Listener | None:
    """The probe listener of this process on LOOM_CELL_PROBE_PORT; a port it cannot bind
    is logged, and the process serves on without it."""
    try:
        sock = bind(settings.cell_probe_port)
    except OSError as e:
        log.error("the probe listener could not bind port %s: %s", settings.cell_probe_port, e)
        return None
    return Listener(sock)
