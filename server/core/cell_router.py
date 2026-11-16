"""The first cell's state as the router's directory answers it (`GET /cells/v1/cells/<cell>`
with the cell's own credential), for the tenancy check and the heartbeat
(docs/architecture/cells.md, "The heartbeat's keys").

Only a process with `LOOM_CELL` set on a first cell asks: a later cell's route answers 404.
The tenancy check reads one of four answers (`directory_answer`): `attaching`, `serving`,
another state (`other`), or `unknown` while no read has landed, the last one failed or
answered for no state of this cell, or it is late (older than the period it is read at plus
`LOOM_CELL_ROUTER_TIMEOUT_S`). Once the process has read `serving` it keeps it for its life
and asks no more: only `loom cell restore` lowers it, and that restarts the processes.

A server process runs one background reader (`start_reader`) that asks every
`min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)`; nothing awaits it, so a
slow router never delays a request or a beat. A process without the reader (a CLI, the
workers child) asks on demand, at most once every `LOOM_TENANCY_MODE_CACHE_S`, the requests
waiting sharing one call.

The reader also keeps what the heartbeat's `router` key reports (`router_key`): `null` until
the first answer, `true` once a 200 named this cell, `false` for a configuration that never
works (a router setting unset, a URL that cannot be requested, a 404, an answer for another
cell, a 401 that persists past `LOOM_ROUTER_DIRECTORY_CACHE_S`, the router replicas'
credential cache), with `router_why` naming which; a transient failure keeps the last value,
and while the key is `null` `router_why` names the last transient cause.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from server.core import cell
from server.settings import settings

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

ATTACHING, SERVING, OTHER, UNKNOWN = "attaching", "serving", "other", "unknown"

# The last answer for the tenancy check and when it landed (monotonic); None before one.
_answer: tuple[str, float] | None = None
# Read `serving` once: kept for the process's life.
_serving = False
# The cause `_ask` gives a 401, which `_keep_router` tells from every other cause.
_UNAUTHORIZED = "401: the router does not accept LOOM_CELL_ROUTER_TOKEN"
# The heartbeat's `router` key and its cause, and when a run of 401 answers began.
_router: bool | None = None
_router_why = ""
_unauthorized_since: float | None = None
# The cause last logged: a run of the same failure logs once.
_logged_why = ""
# Whether this process runs the background reader; without it reads are on demand.
_background = False
_read: asyncio.Task[None] | None = None


def router_url() -> str:
    return settings.cell_router_url.rstrip("/")


def unset() -> list[str]:
    """The router settings a call to it needs and does not have."""
    values = (
        ("LOOM_CELL_ROUTER_URL", router_url()),
        ("LOOM_CELL_ROUTER_TOKEN", settings.cell_router_token),
    )
    return [name for name, value in values if not value]


def period_s() -> float:
    """How often the background reader asks."""
    return min(float(settings.instance_heartbeat_seconds), float(settings.tenancy_mode_cache_s))


def late_after_s() -> float:
    """The age past which an answer counts as `unknown`: the period this process asks at
    (the background reader's, or the on-demand window, LOOM_TENANCY_MODE_CACHE_S), plus the
    time a read may take."""
    every = period_s() if _background else float(settings.tenancy_mode_cache_s)
    return every + float(settings.cell_router_timeout_s)


async def directory_answer() -> str:
    """`attaching`, `serving`, `other` or `unknown` (see the module docstring)."""
    if _serving:
        return SERVING
    if not _background and (
        _answer is None or time.monotonic() - _answer[1] >= settings.tenancy_mode_cache_s
    ):
        await _read_shared()
    if _serving:
        return SERVING
    if _answer is None or time.monotonic() - _answer[1] > late_after_s():
        return UNKNOWN
    return _answer[0]


def router_key() -> dict[str, object]:
    """The heartbeat's `router` and `router_why` keys: present with `LOOM_CELL` set while
    the `first` row is unread (as `null`) and on a first cell, absent otherwise."""
    if not settings.cell:
        return {}
    first = cell.is_first()
    if first is False:
        return {}
    if first is None:
        why = (
            "this cell's first row is absent: run loom cell prepare"
            if cell.rows_read()
            else "the cell's first row is not read yet"
        )
        return {"router": None, "router_why": why}
    return {"router": _router, "router_why": _router_why}


async def _read_shared() -> None:
    """One on-demand read, shared by the requests waiting on it; one cancelled leaves it
    running for the others."""
    global _read
    if _read is None or _read.done():
        _read = asyncio.create_task(read_once())
        # Retrieved here too: every waiter may have gone.
        _read.add_done_callback(lambda t: t.cancelled() or t.exception())
    await asyncio.shield(_read)


async def read_once(client: httpx.AsyncClient | None = None) -> None:
    """Ask the router once, on `client` (the background reader's) or a client of its own,
    and keep the answer."""
    global _answer, _serving, _logged_why
    if client is None:
        import httpx

        async with httpx.AsyncClient() as own:
            state, router, why = await _ask(own)
    else:
        state, router, why = await _ask(client)
    if why != _logged_why:
        if why:
            log.warning("cell state: the router read: %s", why)
        _logged_why = why
    now = time.monotonic()
    _answer = (state, now)
    if state == SERVING:
        _serving = True
    _keep_router(router, why, now)


def _keep_router(router: bool | None, why: str, now: float) -> None:
    """Update the `router` key from one read: `router` None is a transient failure, which
    keeps the last value; a 401 (`why` is `_UNAUTHORIZED`) turns it false only past one
    LOOM_ROUTER_DIRECTORY_CACHE_S of 401s."""
    global _router, _router_why, _unauthorized_since
    if why == _UNAUTHORIZED:
        if _unauthorized_since is None:
            _unauthorized_since = now
        if now - _unauthorized_since <= float(settings.router_directory_cache_s):
            _router_why = "401, possibly a rotation"
            return
        _router, _router_why = False, why
        return
    if router is not None:
        # An answer that proves the configuration ends a run of 401s; a transient one
        # between them does not.
        _unauthorized_since = None
        _router, _router_why = router, why
    elif _router is None:
        _router_why = why


async def _ask(c: httpx.AsyncClient) -> tuple[str, bool | None, str]:
    """(the answer for the tenancy check, the `router` value it proves or None for a
    transient failure, its cause)."""
    if missing := unset():
        return UNKNOWN, False, f"{', '.join(missing)} unset"
    import httpx

    limit = float(settings.cell_router_timeout_s)
    try:
        async with asyncio.timeout(limit):
            r = await c.get(
                f"{router_url()}/cells/v1/cells/{settings.cell}",
                headers={"Authorization": f"Bearer {settings.cell_router_token}"},
                timeout=limit,
            )
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
        # Never fixes itself; the boot refuses most such URLs (`cell.settings_problem`).
        return UNKNOWN, False, f"LOOM_CELL_ROUTER_URL cannot be requested ({type(e).__name__})"
    except TimeoutError:
        return UNKNOWN, None, f"no answer within {limit:g} s"
    except httpx.HTTPError as e:
        return UNKNOWN, None, f"the router did not answer ({type(e).__name__})"
    if r.status_code == 401:
        return UNKNOWN, None, _UNAUTHORIZED
    if r.status_code == 404:
        return UNKNOWN, False, f"404: the router holds no first cell named {settings.cell!r}"
    if r.status_code != 200:
        return UNKNOWN, None, f"the router answered {r.status_code}"
    try:
        answer = r.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("cell"), str):
        return UNKNOWN, None, "the router's 200 is not a JSON object naming the cell"
    if answer["cell"] != settings.cell:
        return UNKNOWN, False, f"the router answered for cell {answer['cell'][:64]!r}"
    state = answer.get("state")
    return (state if state in (ATTACHING, SERVING) else OTHER), True, ""


def start_reader() -> asyncio.Task[None]:
    """The background reader of a server process with `LOOM_CELL` set: it waits for the
    `first` row, asks on a first cell until it has read `serving`, and ends on a later
    cell, which never asks. Its reads share one client, closed when it ends or is
    cancelled at shutdown."""
    global _background
    _background = True

    async def _run() -> None:
        import httpx

        from server.core.ch import set_query_tag

        set_query_tag("cell.router_read")
        client: httpx.AsyncClient | None = None
        try:
            while not _serving:
                first = cell.is_first()
                if first is False:
                    return
                if first:
                    client = client or httpx.AsyncClient()
                    try:
                        await read_once(client)
                    except Exception as e:  # noqa: BLE001 — a failed read must not stop the reader
                        log.warning("cell state: the router read failed: %s", type(e).__name__)
                await asyncio.sleep(period_s())
        finally:
            if client is not None:
                await client.aclose()

    return asyncio.get_running_loop().create_task(_run(), name="loom-cell-router-read")


def _reset_for_tests() -> None:
    global _answer, _serving, _router, _router_why, _unauthorized_since, _background, _read
    global _logged_why
    _answer, _serving, _router, _router_why = None, False, None, ""
    _unauthorized_since, _background, _read, _logged_why = None, False, None, ""
