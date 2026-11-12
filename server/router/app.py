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
- `POST /cells/v1/operator/cells/<cell>/limit` and `.../credential`: the operator's
  changes to a cell's row (`loom cell limit`, `loom cell credential`), with the router's
  operator credential;
- `POST /cells/v1/operator/cells/<cell>/register`: a cell's preparation registers it,
  once the probe listener (the first cell's at LOOM_ROUTER_UPSTREAM_PROBE_URL, a later
  cell's at its `probe_url`) answers its deployment id, prepare's nonces and the bucket
  and prefix it stores under;
- `GET /livez` and `GET /readyz` (the directory answers, read at most once a second).

Every refusal of a placement answers 503 and writes nothing, so the cell's request
answers 503 and its retry is safe; a credential that names no cell, or not this
one, answers 401.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import ssl
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
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
from server.core.cell_probe import PATH as PROBE_PATH
from server.router import directory, routing
from server.router.limits import MAX_KEYS, Limiter
from server.settings import settings

log = logging.getLogger("loom.router")

# A placement or operator call's body is a few short fields: anything larger is refused unread.
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
    # An org with no placement gets the first cell's answer, the same body as an org
    # placed there. Its timing and outage answers can still tell a recently looked-up
    # placed org (cached) from an unplaced one, so an org name is not a secret.
    return JSONResponse({"org": org, "cell": cell, "host": host})


class Router:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        # Where registration's probe goes: None is the network.
        self._probe_transport = probe_transport
        self.placements = Limiter(settings.router_placements_per_minute, clock=clock)
        self.lookups = Limiter(settings.router_lookups_per_minute, clock=clock)
        # The cells' credential hashes, read from the directory at most once per
        # LOOM_ROUTER_DIRECTORY_CACHE_S: a call with a wrong credential reads nothing.
        # A failed read stands for the window too, so an outage is not read once per call.
        self._credentials: dict[str, tuple[str, ...]] = {}
        self._credentials_error = ""
        self._credentials_at: float | None = None
        self._credentials_gen = 0  # bumped when a change makes the cached hashes old
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

    def _forget_credentials(self) -> None:
        self._credentials_gen += 1
        self._credentials_at = None

    async def _credential(self, name: str) -> tuple[str, ...]:
        async with self._lock:
            now = self._clock()
            stale = self._credentials_at is None or (
                now - self._credentials_at >= settings.router_directory_cache_s
            )
            if stale:
                gen = self._credentials_gen
                try:
                    self._credentials = await directory.credentials()
                    self._credentials_error = ""
                except directory.DirectoryUnavailable as e:
                    self._credentials, self._credentials_error = {}, e.reason
                # A read that began before a change is not kept for a window.
                self._credentials_at = now if gen == self._credentials_gen else None
            if self._credentials_error:
                raise directory.DirectoryUnavailable(self._credentials_error)
            return self._credentials.get(name, ())

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
        if not directory.holds(presented, expected):
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
            if not locked.holds(presented):
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
                self._forget_credentials()
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
        if not row.holds(presented):
            self._forget_credentials()  # the cached hash is older than the row
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
                return _refused(503, e.reason, code="BACKEND_UNAVAILABLE")
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

    def _operator(self, request: Request) -> JSONResponse | None:
        """The refusal of a call that does not carry the router's operator credential.
        It is 512 bits (`lco_` and 128 hex), so it is not guessed online; refusals are
        not rate-limited, since behind an ingress one stranger's refusals would lock
        out every operator."""
        expected = settings.router_operator_sha256
        if not expected:
            return _refused(
                503,
                "the router has no operator credential (LOOM_ROUTER_OPERATOR_SHA256)",
                code="NO_OPERATOR_CREDENTIAL",
            )
        token = _bearer(request)
        # Only an operator credential (`lco_`, as `loom cell credential new --operator`
        # makes it): a cell's (`lcr_`) never operates, even if its hash were configured here.
        presented = hashlib.sha256(token.encode()).hexdigest()
        if token.startswith("lco_") and hmac.compare_digest(presented, expected):
            return None
        return _refused(401, "the operator credential is not the router's")

    async def set_limit(self, request: Request) -> JSONResponse:
        """`loom cell limit`: `{"closed": bool}` and/or `{"placements_max": int | null}`."""
        if refusal := self._operator(request):
            return refusal
        if refusal := _malformed_cell_refusal(request):
            return refusal
        body = await _body(request)
        known = {"closed", "placements_max"}
        if not isinstance(body, dict) or not body or set(body) - known:
            return _refused(400, "a limit is a JSON object of closed and/or placements_max")
        closed = body.get("closed", directory.UNSET)
        quota = body.get("placements_max", directory.UNSET)
        if "closed" in body and not isinstance(closed, bool):
            return _refused(400, "closed is true or false")
        # `type(...) is int`: `true` is not a count of 1.
        counted = type(quota) is int and 0 <= quota <= cell_state.PLACEMENTS_MAX
        if quota is not directory.UNSET and quota is not None and not counted:
            return _refused(400, "placements_max is a whole number, 0 or more, or null")
        return await _operator_answer(
            lambda: directory.set_limit(
                request.path_params["cell"], closed=closed, placements_max=quota
            )
        )

    async def rotate_credential(self, request: Request) -> JSONResponse:
        """`loom cell credential`: `{"step": "next", "sha256": ...}`, `{"step": "finish"}`
        or `{"step": "revert"}`."""
        if refusal := self._operator(request):
            return refusal
        if refusal := _malformed_cell_refusal(request):
            return refusal
        body = await _body(request)
        if not isinstance(body, dict) or set(body) - {"step", "sha256"}:
            return _refused(400, "a rotation is a JSON object of step and sha256")
        step, sha = body.get("step"), body.get("sha256", "")
        if step not in ("next", "finish", "revert") or (step == "next") != ("sha256" in body):
            return _refused(400, "a rotation step is next (with sha256), finish or revert")
        if step == "next" and not (isinstance(sha, str) and cell_state.SHA256.fullmatch(sha)):
            return _refused(400, "sha256 is 64 lowercase hex characters: the credential's hash")
        if step == "next" and _is_operator_hash(sha):
            return _refused(400, _OPERATOR_HASH_REFUSAL)
        response = await _operator_answer(
            lambda: directory.rotate_credential(request.path_params["cell"], step, sha)
        )
        # This replica reads the hashes again on its next call. A 503 is included: its
        # change may have committed (BACKEND_UNAVAILABLE).
        if response.status_code in (200, 503):
            self._forget_credentials()
        return response

    async def register(self, request: Request) -> JSONResponse:
        """A cell's preparation: register the cell once its probe address shows the stores
        prepare wrote its nonces to are the ones the cell serves from. The first cell
        (its host is the shared host) is probed at LOOM_ROUTER_UPSTREAM_PROBE_URL, never an
        address prepare names; a later cell at its own `probe_url`."""
        if refusal := self._operator(request):
            return refusal
        if refusal := _malformed_cell_refusal(request):
            return refusal
        parsed = _registration(request.path_params["cell"], await _body(request))
        if isinstance(parsed, JSONResponse):
            return parsed
        reg, nonces = parsed
        try:
            if refused := await directory.refusal_before_probe(reg):
                return _refused(409, refused)
        except directory.DirectoryUnavailable as e:
            return _refused(503, e.reason, code="BACKEND_UNAVAILABLE")
        if refusal := await _probe(reg, nonces, self._probe_transport):
            return refusal
        # Only a first cell has an upstream, and a rerun may come with none: it copies none.
        if reg.upstream_url and (refusal := await _upstream_check(reg, self._probe_transport)):
            return refusal
        try:
            row, written = await directory.register(reg)
        except directory.OperatorRefused as e:
            return _refused(409, e.reason)
        except directory.DirectoryUnavailable as e:
            # Its commit may have landed: this replica reads the hashes again.
            self._forget_credentials()
            return _refused(503, e.reason, code="BACKEND_UNAVAILABLE")
        self._forget_credentials()  # its credential places from this replica at once
        return JSONResponse(
            {
                "cell": row.cell,
                "is_first": row.is_first,
                "state": row.state,
                "mode": row.mode,
                "registered": written,
            }
        )

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


def _malformed_cell_refusal(request: Request) -> JSONResponse | None:
    """The refusal of a cell name no cell can have, before the directory is asked."""
    if not cell_state.NAME.fullmatch(request.path_params["cell"]):
        return _refused(404, "no cell has that name")
    return None


# A probe answers from memory or reads three keys: a few seconds is ample.
PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)  # not a setting: the probe's shape bounds it
PROBE_DEADLINE_S = 15.0  # not a setting: the whole probe, however slowly it answers
# The longest a registration's answer can take: the read before the probe, the probe and
# the first cell's upstream check, then the write. A client waits at least this long.
REGISTER_ANSWER_MAX_S = (  # not a setting: derived
    directory.REFUSAL_READ_MAX_S + 2 * PROBE_DEADLINE_S + directory.REGISTER_MAX_S
)
_SECONDS = re.compile(r"[0-9]{1,6}")  # ASCII digits: str.isdigit takes any script's
_TEXT_MAX = 1024  # not a setting: a host, an id, a bucket name or prefix is far shorter
_NONCE_MAX = 128  # not a setting: prepare's nonces are 32 hex characters
# The listener echoes the deployment id, bucket and prefix (each at most _TEXT_MAX) and
# three nonces (each at most _NONCE_MAX), 6 bytes a character at most as JSON; the rest
# is room for the keys.
PROBE_ANSWER_MAX = 6 * (3 * _TEXT_MAX + 3 * _NONCE_MAX) + 4096  # not a setting: derived
_NONCES = ("postgres", "clickhouse", "bucket")


def _text(body: dict[str, Any], key: str, *, needed: bool = False) -> str | None:
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > _TEXT_MAX or "\x00" in value:
        return None
    try:
        value.encode()  # a lone surrogate: malformed, never a directory outage
    except UnicodeEncodeError:
        return None
    return value if value or not needed else None


def _nonce(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _NONCE_MAX and value.isascii()


_OPERATOR_HASH_REFUSAL = (
    "that is the operator credential's hash: a cell's credential must be its own "
    "(`loom cell credential new`)"
)


def _is_operator_hash(sha: str) -> bool:
    """Whether `sha` is the router's operator credential's: a cell holding it could make
    every operator change."""
    return hmac.compare_digest(sha, settings.router_operator_sha256)


def _cell_address(value: str) -> str | None:
    """A cell's probe or upstream address in the shape the forward's upstreams take
    (`http(s)://host[:port]`, `routing.usable_upstream`), written one way however it is
    spelled (host in lower case, IDNA-encoded, an IPv6 address compressed; no default
    port and no trailing `/`; a fully qualified name keeps its final dot, as the forward
    resolves it), so replicas that spell one address differently agree on it; the router
    appends the path."""
    if not routing.usable_upstream(value):
        return None
    u = httpx.URL(value)
    # httpx lower-cases a name; an IPv6 address is compressed (lower-cased) below.
    host = u.raw_host.decode("ascii")
    if ":" in host:  # an IPv6 address, which httpx gives without its brackets
        host = f"[{ipaddress.ip_address(host).compressed}]"
    default = {"http": 80, "https": 443}[u.scheme]
    return f"{u.scheme}://{host}" + ("" if u.port in (None, default) else f":{u.port}")


def _registration(
    cell: str, body: Any
) -> tuple[directory.Registration, dict[str, str | None]] | JSONResponse:
    known = {"host", "deployment_id", "credential_sha256", "bucket", "bucket_prefix"}
    if not isinstance(body, dict) or set(body) - known - {"probe_url", "nonces"}:
        return _refused(
            400,
            "a registration is a JSON object of " + ", ".join(sorted(known)) + " and nonces, "
            "with a later cell's probe_url",
        )
    host_text = _text(body, "host", needed=True)
    deployment_id = _text(body, "deployment_id", needed=True)
    sha = _text(body, "credential_sha256", needed=True)
    bucket, prefix = _text(body, "bucket"), _text(body, "bucket_prefix")
    probe = _text(body, "probe_url")
    if (
        host_text is None
        or deployment_id is None
        or bucket is None
        or prefix is None
        or probe is None
    ):
        return _refused(
            400,
            f"host and deployment_id are required; each field is text of at most {_TEXT_MAX} "
            "characters, with no NUL and no lone surrogate",
        )
    host = cell_state.normalise_host(host_text)
    # A host is written once and never moved: the address rule, one spelling.
    if not routing.usable_host(host):
        return _refused(400, "host names no valid host")
    if sha is None or not cell_state.SHA256.fullmatch(sha):
        return _refused(400, "credential_sha256 is 64 lowercase hex characters: the hash")
    if _is_operator_hash(sha):
        return _refused(400, _OPERATOR_HASH_REFUSAL)
    nonces = body.get("nonces")
    if not (
        isinstance(nonces, dict)
        and set(nonces) == set(_NONCES)
        and _nonce(nonces["postgres"])
        and _nonce(nonces["clickhouse"])
        and (nonces["bucket"] is None if not bucket else _nonce(nonces["bucket"]))
    ):
        return _refused(
            400,
            "nonces names postgres, clickhouse and bucket (null with no bucket), each ASCII "
            f"text of 1 to {_NONCE_MAX} characters",
        )
    if prefix and not bucket:
        return _refused(400, "a bucket_prefix is named with its bucket")
    if bucket and (not prefix or prefix != prefix.strip("/")):
        # A cell's own settings refuse these (LOOM_DOCUMENT_S3_PREFIX), so no listener
        # could echo them back.
        return _refused(400, "a bucket's prefix is named, with no leading or trailing /")
    probe_address = _cell_address(probe) if probe else ""
    if probe_address is None:
        return _refused(400, "probe_url, when given, is http(s)://host[:port] with a valid host")
    # What needs no configuration is judged above; what depends on the first cell, below.
    shared_host = cell_state.normalise_host(settings.router_shared_host)
    if not shared_host:  # unset, or no host it can read (`loom router serve` refuses that)
        return _refused(
            409,
            "the router needs LOOM_ROUTER_SHARED_HOST, a valid host, to tell the first cell "
            "from a later one",
        )
    is_first = host == shared_host
    upstream = ""
    if is_first:
        if probe:
            return _refused(
                400, "the first cell is probed at the router's own address: no probe_url"
            )
        # The router's own configuration, never prepare's: the deployment it fronts.
        address = _cell_address(settings.router_upstream_probe_url) or ""
        if not address:
            return _refused(
                409,
                "the router needs LOOM_ROUTER_UPSTREAM_PROBE_URL (http or https with a host) "
                "to register the first cell",
            )
        # The upstream may be '': only a new row copies it (the directory refuses one
        # without it), and a rerun probes and answers without it.
        upstream = _cell_address(settings.router_upstream_url) or ""
    else:
        if not probe_address:
            return _refused(400, "a later cell names its probe_url")
        address = probe_address
    reg = directory.Registration(
        cell=cell,
        host=host,
        deployment_id=deployment_id,
        credential_sha256=sha,
        bucket=bucket,
        bucket_prefix=prefix,
        probe_url=address,
        is_first=is_first,
        upstream_url=upstream,
    )
    return reg, dict(nonces)


@functools.cache
def _trusted_cas() -> ssl.SSLContext:
    """The certificate authorities the forward trusts (SSL_CERT_FILE, SSL_CERT_DIR): the
    probe reads no proxy from the environment, but a cell behind a private CA is reached
    as the forward reaches it. Built once: the environment it reads is the process's, and
    reading the bundle again would block the loop on every probe."""
    return httpx.create_ssl_context(trust_env=True)


async def _fetch(
    url: str, transport: httpx.AsyncBaseTransport | None
) -> tuple[int, str, Any] | None:
    """GET `url` within the probe's bounds: its status, Retry-After and JSON answer (None
    when it is no JSON, or past the cap); None when it could not be reached. The whole
    call is bounded, and so is the answer's size: it is asked uncompressed and an encoded
    answer is read as none, so the cap counts what was received."""
    try:
        async with asyncio.timeout(PROBE_DEADLINE_S):
            async with (
                httpx.AsyncClient(
                    timeout=PROBE_TIMEOUT,
                    trust_env=False,
                    verify=_trusted_cas(),
                    transport=transport,
                ) as client,
                client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as r,
            ):
                status, retry_after = r.status_code, r.headers.get("retry-after", "")
                # Never inflated: an encoded answer is no listener's, which sends none.
                encoded = r.headers.get("content-encoding", "identity") not in ("", "identity")
                raw = b""
                if not encoded:
                    async for chunk in r.aiter_bytes():
                        raw += chunk
                        if len(raw) > PROBE_ANSWER_MAX:
                            break
    except (httpx.HTTPError, TimeoutError) as e:
        # The error's text goes to the router's log only (a certificate it cannot verify
        # reads as unreachable to the caller): never into an answer.
        log.warning("register: %s unreachable: %s: %s", url, type(e).__name__, e)
        return None
    try:
        answer = json.loads(raw) if not encoded and len(raw) <= PROBE_ANSWER_MAX else None
    except (ValueError, RecursionError):  # nested past the parser's depth
        answer = None
    return status, retry_after, answer


async def _probe(
    reg: directory.Registration,
    nonces: dict[str, str | None],
    transport: httpx.AsyncBaseTransport | None,
) -> JSONResponse | None:
    """None when the probe listener at the cell's probe address answers its deployment
    id, the nonces prepare wrote and the bucket and prefix it names, else the refusal. A
    busy listener answers 503 `PROBE_BUSY` with its Retry-After; an unreachable one, or
    one that cannot read a store, 503 `PROBE_UNREACHABLE`: prepare waits and asks again."""
    fetched = await _fetch(reg.probe_url + PROBE_PATH, transport)
    if fetched is None:
        return _refused(503, "the probe address could not be reached", code="PROBE_UNREACHABLE")
    status, retry_after, answer = fetched
    if status == 429:
        # Passed on only as a short count of seconds: the listener's text is not ours.
        # At least 1: a 0 would have prepare ask again at once.
        ok = _SECONDS.fullmatch(retry_after) and int(retry_after) > 0
        retry_after = retry_after if ok else "1"
        return _refused(
            503, "the probe listener is busy", {"Retry-After": retry_after}, code="PROBE_BUSY"
        )
    if status == 503 and isinstance(answer, dict) and answer.get("store") in _NONCES:
        # The listener's own answer names the store it could not read.
        store = answer["store"]
        return _refused(503, f"the probe listener could not read {store}", code="PROBE_UNREACHABLE")
    if status in (502, 503, 504):  # a gateway in front of a listener it could not reach
        return _refused(503, "the probe address could not be reached", code="PROBE_UNREACHABLE")
    if status != 200 or not isinstance(answer, dict):
        return _refused(409, f"the probe address did not answer as a probe listener ({status})")
    if answer.get("deployment_id") != reg.deployment_id:
        return _refused(409, "the probe address reports another deployment id")
    if "nonces" not in answer:
        # The main port answers the same path with the id alone.
        return _refused(
            409,
            "the probe address answered as a main port, not the probe listener "
            "(LOOM_CELL_PROBE_PORT)",
        )
    # Before the nonces: a listener storing elsewhere reads no bucket nonce, and its bucket
    # names what to fix.
    if answer.get("bucket") != {"name": reg.bucket, "prefix": reg.bucket_prefix}:
        return _refused(409, "the probe listener stores under another bucket or prefix")
    if answer.get("nonces") != nonces:
        return _refused(409, "the probe listener does not read the stores prepare wrote to")
    return None


async def _upstream_check(
    reg: directory.Registration, transport: httpx.AsyncBaseTransport | None
) -> JSONResponse | None:
    """None when the first cell's upstream (the deployment the shared host forwards to)
    reports the deployment id its probe listener did, else the refusal: registration
    copies the upstream into the row, so it must be the same deployment. The main port
    answers the id from memory and sets no rate of its own: a 429, 502, 503 or 504 there
    is a proxy's in front of it, and is asked again."""
    fetched = await _fetch(reg.upstream_url + PROBE_PATH, transport)
    if fetched is None or fetched[0] in (429, 502, 503, 504):
        return _refused(
            503, "LOOM_ROUTER_UPSTREAM_URL could not be reached", code="PROBE_UNREACHABLE"
        )
    status, _retry_after, answer = fetched
    if status != 200 or not isinstance(answer, dict):
        return _refused(409, f"LOOM_ROUTER_UPSTREAM_URL did not answer as Loom ({status})")
    if "nonces" in answer:
        # The probe listener answers the same path with its nonces.
        return _refused(
            409,
            "LOOM_ROUTER_UPSTREAM_URL answered as the probe listener, not the main port",
        )
    if answer.get("deployment_id") == "":
        # A serving process has verified its deployment at boot, so '' never changes: it
        # runs without the Postgres authority, which a cell needs.
        return _refused(
            409,
            "LOOM_ROUTER_UPSTREAM_URL reports no deployment id: it runs without the "
            "Postgres authority (LOOM_STORAGE_AUTHORITY=postgres), which a cell needs",
        )
    if answer.get("deployment_id") != reg.deployment_id:
        return _refused(
            409,
            "LOOM_ROUTER_UPSTREAM_URL reports another deployment id than "
            "LOOM_ROUTER_UPSTREAM_PROBE_URL: they must name one deployment",
        )
    return None


async def _operator_answer(change: Callable[[], Awaitable[directory.Cell]]) -> JSONResponse:
    try:
        row = await change()
    except directory.NoSuchCell as e:
        return _refused(404, e.reason)
    except directory.OperatorRefused as e:
        return _refused(409, e.reason)
    except directory.DirectoryUnavailable as e:
        # The change may have committed before the answer was lost (a COMMIT that waited
        # past its bound on the standby): the CLI matches this code.
        return _refused(503, e.reason, code="BACKEND_UNAVAILABLE")
    return JSONResponse(
        {
            "cell": row.cell,
            "closed": row.closed,
            "placements_max": row.placements_max,
            "placed_orgs": row.placed,
            "next_credential_pending": bool(row.credential_next_sha256),
        }
    )


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
            Route("/cells/v1/operator/cells/{cell}/limit", router.set_limit, methods=["POST"]),
            Route("/cells/v1/operator/cells/{cell}/register", router.register, methods=["POST"]),
            Route(
                "/cells/v1/operator/cells/{cell}/credential",
                router.rotate_credential,
                methods=["POST"],
            ),
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
