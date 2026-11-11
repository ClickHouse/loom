"""The router's own answers (docs/architecture/cells.md, "The router"):

- `POST /cells/v1/placements`: a cell places a new org, with its own credential
  (`LOOM_CELL_ROUTER_TOKEN` there, its SHA-256 in the directory) and its
  configuration fingerprint, and gets back the cell and deployment id that hold it;
- `GET /cells/v1/cells/<cell>`: the first cell's state (`attaching`, `serving`), for that
  cell's own credential only, which it reads while its mode is `recording`;
- `GET /cells/lookup?org=`: which cell, and at which host, an org lives in
  (unauthenticated, rate-limited per client address); a placed org's answer is cached
  and given while the directory cannot be read, and before any cell is registered the
  lookup answers 503 with the code `NO_CELL`;
- `GET /livez` and `GET /readyz` (the directory answers, read at most once a second).

Every refusal of a placement answers 503 and writes nothing, so the cell's request
answers 503 and its retry is safe; a credential that names no cell, or not this
one, answers 401.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, NamedTuple
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from server.auth.tenancy import ORG_FREE
from server.core import cell as cell_state
from server.router import directory, routing
from server.router.limits import MAX_KEYS, Limiter
from server.settings import settings

log = logging.getLogger("loom.router")

# A placement call's body is three short fields: anything larger is refused unread.
MAX_BODY_BYTES = 64 * 1024  # not a setting: the request's own shape bounds it
# How long a readiness answer stands: a probe every second reads the directory once.
READY_TTL_S = 1.0  # not a setting: the probe's own cadence bounds it


def _refused(
    status: int, reason: str, headers: dict[str, str] | None = None, *, code: str = ""
) -> JSONResponse:
    # A client matches `code`, never the message.
    body = {"error": reason, **({"code": code} if code else {})}
    return JSONResponse(body, status_code=status, headers=headers)


def _org(value: Any) -> str | None:
    # A name Postgres text cannot hold (a NUL, a lone surrogate) is malformed, never a
    # directory outage.
    if not isinstance(value, str) or value in ORG_FREE or "\x00" in value:
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    return value


def _bearer(request: Request) -> str:
    return routing.bearer(request.headers.get("authorization", ""))


async def _bounded(request: Request) -> bytes | None:
    """The request body, or None past MAX_BODY_BYTES (refused without reading it all)."""
    size = request.headers.get("content-length")
    if size is not None and (not size.isdigit() or int(size) > MAX_BODY_BYTES):
        return None
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            return None
    return raw


async def _body(request: Request) -> Any:
    raw = await _bounded(request)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):  # not JSON, or nested past the parser's depth
        return None


def placement_refusal(row: directory.Cell) -> str:
    """Why the cell may not place a new org now ('' when it may). It places while it
    is `serving`, or as the first cell while `attaching` once the directory records
    `enforced` (attach step 3), and is not closed."""
    attached = row.state == "serving" or (
        row.is_first and row.state == "attaching" and row.mode == "enforced"
    )
    if not attached:
        return f"cell {row.cell!r} is not attached yet"
    if row.closed:
        return f"cell {row.cell!r} is closed to new orgs"
    return ""


def fingerprint_matches(row: directory.Cell, posted: Any) -> bool:
    """The calling process's fingerprint agrees with a value of the cell's row."""
    if not isinstance(posted, dict) or not _this_version(posted):
        return False
    fields = posted.get("fields")
    if not isinstance(fields, dict):
        return False
    return _agrees(fields, row.current_fp) or _agrees(fields, row.next_fp)


def _agrees(fields: dict[str, Any], value: Any) -> bool:
    """Every field the row's value or this build names is equal, a missing one at its
    default; a field this build does not know and the caller does not send is a newer
    build's, judged by the cell's own gate."""
    if not isinstance(value, dict) or not _this_version(value):
        return False
    theirs = value.get("fields")
    if not isinstance(theirs, dict):
        return False
    for name in set(theirs) | set(cell_state.DEFAULTS):
        if name not in fields and name not in cell_state.DEFAULTS:
            continue
        default = cell_state.DEFAULTS.get(name)
        got, want = fields.get(name, default), theirs.get(name, default)
        if type(got) is not type(want) or got != want:  # `1` is not `true`
            return False
    return True


def _this_version(value: dict[str, Any]) -> bool:
    # An int, as the cell's own gate reads it: `true` and `1.0` are not version 1.
    version = value.get("v")
    return type(version) is int and version == cell_state.FINGERPRINT_VERSION


class _Placed(NamedTuple):
    cell: str
    host: str
    read_at: float  # monotonic


def _answer(org: str, cell: str, host: str) -> JSONResponse:
    return JSONResponse({"org": org, "cell": cell, "host": host})


class Router:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.placements = Limiter(settings.router_placements_per_minute, clock=clock)
        self.lookups = Limiter(settings.router_lookups_per_minute, clock=clock)
        # The cells' credential hashes, read from the directory at most once per
        # LOOM_ROUTER_DIRECTORY_CACHE_S: a call with a wrong credential reads nothing.
        # A failed read stands for the window too, so an outage is not read once per call.
        self._credentials: dict[str, str] = {}
        self._credentials_error = ""
        self._credentials_at: float | None = None
        self._ready_at: float | None = None
        self._ready = ""
        self._lock = asyncio.Lock()
        self._ready_lock = asyncio.Lock()
        # Placed orgs' (cell, host), read again once LOOM_ROUTER_DIRECTORY_CACHE_S old and
        # answered stale while the directory cannot be read: a placement never changes, so
        # an outage costs only uncached lookups. Bounded: the oldest read is forgotten.
        self._placed: OrderedDict[str, _Placed] = OrderedDict()
        # The routing table, read like the credential hashes: at most once per window,
        # and the last one kept while the directory cannot be read.
        self._table: routing.Table | None = None
        self._table_at: float | None = None
        self._table_lock = asyncio.Lock()
        self._refreshing: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None

    async def _credential(self, name: str) -> str | None:
        async with self._lock:
            now = self._clock()
            stale = self._credentials_at is None or (
                now - self._credentials_at >= settings.router_directory_cache_s
            )
            if stale:
                try:
                    self._credentials = await directory.credentials()
                    self._credentials_error = ""
                except directory.DirectoryUnavailable as e:
                    self._credentials, self._credentials_error = {}, e.reason
                self._credentials_at = now
            if self._credentials_error:
                raise directory.DirectoryUnavailable(self._credentials_error)
            return self._credentials.get(name)

    async def _authenticated(self, request: Request, name: str) -> str | JSONResponse:
        """The SHA-256 of the bearer when it is cell `name`'s credential as the router
        caches it, else the refusal: 401, or 503 while the directory cannot be read. A
        wrong credential reads nothing; each route checks the hash against its row too."""
        token = _bearer(request)
        if not token or not cell_state.NAME.fullmatch(name):
            return _refused(401, "the credential names no cell of this directory")
        try:
            expected = await self._credential(name)
        except directory.DirectoryUnavailable as e:
            return _refused(503, e.reason)
        presented = hashlib.sha256(token.encode()).hexdigest()
        if expected is None or not hmac.compare_digest(presented, expected):
            return _refused(401, "the credential names no cell of this directory")
        return presented

    async def place(self, request: Request) -> JSONResponse:
        if not _bearer(request):
            return _refused(401, "the credential names no cell of this directory")
        body = await _body(request)
        if not isinstance(body, dict):
            return _refused(400, "a placement is a JSON object: org, cell, fingerprint")
        org, name = _org(body.get("org")), body.get("cell")
        if org is None or not isinstance(name, str) or not cell_state.NAME.fullmatch(name):
            return _refused(400, "a placement names an org and a cell")
        presented = await self._authenticated(request, name)
        if isinstance(presented, JSONResponse):
            return presented
        if not self.placements.admit(name):
            wait = str(self.placements.wait_s(name))
            return _refused(
                503, f"cell {name!r} placed too many orgs this minute", {"Retry-After": wait}
            )
        posted = body.get("fingerprint")
        replaced = False

        def refusal(locked: directory.Cell) -> str:
            # Judged on the row as locked for the placement, so a cell closed, limited
            # or updated meanwhile is judged as it is now.
            nonlocal replaced
            if not hmac.compare_digest(presented, locked.credential_sha256):
                replaced = True
                return f"cell {name!r} has another credential now"
            if reason := placement_refusal(locked):
                return reason
            if not fingerprint_matches(locked, posted):
                return f"the calling process's configuration matches no value of {name!r}"
            return ""

        try:
            winner, deployment_id = await directory.place(org, name, refusal)
        except (directory.PlacementRefused, directory.DirectoryUnavailable) as e:
            if replaced:
                # The cached hash is older than the row: read the directory again on
                # the next call, and answer as for any credential the row does not hold.
                self._credentials_at = None
                return _refused(401, "the credential names no cell of this directory")
            return _refused(503, e.reason)
        log.info("placement of %s: %s (asked by %s)", org, winner, name)
        return JSONResponse({"cell": winner, "deployment_id": deployment_id})

    async def cell_state(self, request: Request) -> JSONResponse:
        """`GET /cells/v1/cells/<cell>`: the first cell's state, for its own credential
        only. A first cell in `recording` records a new org only while this answers
        `attaching`; any other answer counts as unreadable there."""
        name = request.path_params["cell"]
        presented = await self._authenticated(request, name)
        if isinstance(presented, JSONResponse):
            return presented
        try:
            row = await directory.first_cell_named(name)
        except directory.DirectoryUnavailable as e:
            return _refused(503, e.reason)
        if row is None:
            return _refused(404, f"cell {name!r} is not the first cell")
        if not hmac.compare_digest(presented, row.credential_sha256):
            self._credentials_at = None  # the cached hash is older than the row
            return _refused(401, "the credential names no cell of this directory")
        return JSONResponse({"cell": row.cell, "state": row.state})

    async def lookup(self, request: Request) -> JSONResponse:
        client = request.client.host if request.client else ""
        if not self.lookups.admit(client):
            wait = str(self.lookups.wait_s(client))
            return _refused(429, "too many lookups", {"Retry-After": wait})
        org = _org(request.query_params.get("org"))
        if org is None:
            return _refused(400, "a lookup names an org: ?org=")
        cached = self._placed.get(org)
        now = self._clock()
        if cached is not None and now - cached.read_at < settings.router_directory_cache_s:
            return _answer(org, cached.cell, cached.host)
        if cached is not None:
            # Stamped before the read: lookups arriving while it runs, and after it fails,
            # get the cached answer, so a down directory is read at most once a window.
            self._remember(org, cached.cell, cached.host, now)
        try:
            found = await directory.lookup(org)
            if found is not None:
                self._remember(org, *found, now)
            else:
                first = await directory.first_cell()
                found = (first.cell, first.host) if first else None
        except directory.DirectoryUnavailable as e:
            if cached is None:
                return _refused(503, e.reason)
            return _answer(org, cached.cell, cached.host)
        if found is None:
            return _refused(503, "the directory holds no cell yet", code="NO_CELL")
        return _answer(org, *found)

    def _remember(self, org: str, cell: str, host: str, now: float) -> None:
        self._placed.pop(org, None)
        self._placed[org] = _Placed(cell, host, now)
        while len(self._placed) > MAX_KEYS:
            self._placed.popitem(last=False)

    async def _routing_table(self) -> routing.Table | None:
        """The table, read at most once per window. Once there is one it is answered at
        once and a stale one is read again in the background, so a directory that hangs
        delays no request; a failed read keeps the last table and stands for the window."""
        fresh = self._table_at is not None and (
            self._clock() - self._table_at < settings.router_directory_cache_s
        )
        if fresh:
            return self._table
        if self._table is not None:
            self._refreshing = self._refreshing or asyncio.create_task(self._refresh())
            return self._table
        await self._refresh()
        return self._table

    async def _refresh(self) -> None:
        try:
            async with self._table_lock:
                fresh = self._table_at is not None and (
                    self._clock() - self._table_at < settings.router_directory_cache_s
                )
                if fresh:  # read by the request this one waited behind
                    return
                try:
                    cells, aliases = await directory.routing()
                except directory.DirectoryUnavailable as e:
                    log.warning("routing: directory unreadable, keeping the last table: %s", e)
                else:
                    self._table = routing.build(
                        cells,
                        aliases,
                        shared_host=settings.router_shared_host,
                        passthrough=settings.router_upstream_url,
                    )
                self._table_at = self._clock()  # when the read ended, failed or not
        finally:
            self._refreshing = None

    async def forward(self, request: Request) -> Response:
        """Rules 1 to 4: every request that is not the router's own goes to one cell,
        forwarded as the client sent it (raw path and query), with the router's own
        `X-Forwarded-*` values, both bodies streamed."""
        table = await self._routing_table()
        if table is None:
            return _refused(503, "the router has not read its directory yet")
        host = request.headers.get("host", "")
        body: bytes | None = None
        revoked = ""
        if request.method == "POST" and routing.reads_revoked(table, host, request.url.path):
            body = await _bounded(request)
            if body is None:
                return _refused(413, "a token revocation is a short form")
            # The last `token`, as the cell's own form reading takes it.
            form = parse_qs(body.decode("latin-1"), keep_blank_values=True)
            revoked = (form.get("token") or [""])[-1]
        decision = routing.decide(
            table,
            host=host,
            path=request.url.path,
            authorization=request.headers.get("authorization", ""),
            revoked=revoked,
        )
        if not decision.upstream:
            # A 401 says how to recover, as a cell's own does (RFC 6750 3).
            challenge = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
            headers = challenge if decision.status == 401 else None
            return _refused(decision.status, decision.reason, headers)
        return await self._proxy(request, decision.upstream, body)

    async def _proxy(self, request: Request, upstream: str, body: bytes | None) -> Response:
        if self._http is None:
            self._http = proxy_client()
        dropped = _HOP_BY_HOP | _FORWARDED | _named_by_connection(request.headers.raw)
        headers = [(k, v) for k, v in request.headers.raw if k.lower() not in dropped]
        # The router's own view of the client, never what the client claims: uvicorn has
        # already resolved a trusted balancer's chain into `request.client`.
        headers += [
            (b"x-forwarded-for", (request.client.host if request.client else "").encode()),
            (b"x-forwarded-proto", request.url.scheme.encode()),
            (b"x-forwarded-host", request.headers.get("host", "").encode()),
        ]
        if body is None and not (
            request.headers.get("content-length") or request.headers.get("transfer-encoding")
        ):
            body = b""  # no body was sent: none is framed (no chunked empty body)
        raw_path: bytes = request.scope.get("raw_path") or request.url.path.encode()
        query: bytes = request.scope.get("query_string") or b""
        try:
            url = httpx.URL(upstream).copy_with(
                raw_path=raw_path + (b"?" + query if query else b"")
            )
        except httpx.InvalidURL:
            return _refused(400, "the request's path is not one the router can forward")
        outbound = self._http.build_request(
            request.method,
            url,
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers],
            content=body if body is not None else request.stream(),
        )
        try:
            answer = await self._http.send(outbound, stream=True)
        except httpx.HTTPError as e:
            log.warning("routing: %s unreachable: %s", upstream, type(e).__name__)
            return _refused(502, "the cell this request goes to could not be reached")
        dropped = _HOP_BY_HOP | _named_by_connection(answer.headers.raw)
        response = StreamingResponse(
            answer.aiter_raw(),
            status_code=answer.status_code,
            background=BackgroundTask(answer.aclose),
        )
        # Every header line as the cell sent it: a second Set-Cookie is not a duplicate.
        response.raw_headers = [(k, v) for k, v in answer.headers.raw if k.lower() not in dropped]
        return response

    async def close(self) -> None:
        if self._refreshing is not None:
            self._refreshing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refreshing
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def livez(self, _request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(self, _request: Request) -> JSONResponse:
        async with self._ready_lock:
            now = self._clock()
            if self._ready_at is None or now - self._ready_at >= READY_TTL_S:
                try:
                    await directory.ping()
                    self._ready = ""
                except directory.DirectoryUnavailable as e:
                    self._ready = e.reason
                self._ready_at = now
            reason = self._ready
        if reason:
            return _refused(503, reason)
        return JSONResponse({"status": "ok"})


# Headers that describe one connection, not the message (RFC 9110 7.6.1).
_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
# What a client claims about where it came from: the router writes its own.
_FORWARDED = frozenset(
    {b"x-forwarded-for", b"x-forwarded-proto", b"x-forwarded-host", b"forwarded"}
)
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _named_by_connection(raw: list[tuple[bytes, bytes]]) -> frozenset[bytes]:
    """The headers a `Connection` header names, which describe the hop too."""
    return frozenset(
        name.strip().lower()
        for k, v in raw
        if k.lower() == b"connection"
        for name in v.split(b",")
        if name.strip()
    )


async def _own(_request: Request) -> JSONResponse:
    return _refused(405, "the router answers this path with GET only")


# A connect bound so a gone cell answers 502 soon, no read bound (an event stream may
# idle), and no bound on open connections, so a request never waits for a pool slot.
PROXY_TIMEOUT = httpx.Timeout(None, connect=5.0)  # not a setting: cells share the network
PROXY_LIMITS = httpx.Limits(  # not a setting: 100 idle connections kept, not a cap
    max_connections=None, max_keepalive_connections=100
)


def proxy_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The client the forward uses (PROXY_TIMEOUT, PROXY_LIMITS)."""
    return httpx.AsyncClient(transport=transport, timeout=PROXY_TIMEOUT, limits=PROXY_LIMITS)


async def _not_found(_request: Request) -> JSONResponse:
    return _refused(404, "the router answers no such path")


def build_app(router: Router | None = None) -> Starlette:
    router = router or Router()

    @asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await router.close()
            await directory.close()

    return Starlette(
        routes=[
            Route("/cells/v1/placements", router.place, methods=["POST"]),
            Route("/cells/v1/cells/{cell}", router.cell_state, methods=["GET"]),
            Route("/cells/lookup", router.lookup, methods=["GET"]),
            Route("/livez", router.livez, methods=["GET"]),
            Route("/readyz", router.readyz, methods=["GET"]),
            # Its own paths with any other method: never forwarded.
            Route("/livez", _own, methods=_METHODS),
            Route("/readyz", _own, methods=_METHODS),
            # `/cells/` is the router's own: a path it does not answer is not forwarded.
            Route("/cells/{rest:path}", _not_found, methods=_METHODS),
            Route("/{path:path}", router.forward, methods=_METHODS),
        ],
        lifespan=_lifespan,
    )
