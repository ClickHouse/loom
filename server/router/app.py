"""The router's own answers (docs/architecture/cells.md, "The router"):

- `POST /cells/v1/placements`: a cell places a new org, with its own credential
  (`LOOM_CELL_ROUTER_TOKEN` there, its SHA-256 in the directory) and its
  configuration fingerprint, and gets back the cell and deployment id that hold it;
- `GET /cells/lookup?org=`: which cell, and at which host, an org lives in
  (unauthenticated, rate-limited per client address);
- `GET /livez` and `GET /readyz` (the directory answers, read at most once a second).

Every refusal of a placement answers 503 and writes nothing, so the cell's request
answers 503 and its retry is safe; a credential that names no cell, or not this
one, answers 401.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from server.auth.tenancy import ORG_FREE
from server.core import cell as cell_state
from server.router import directory
from server.router.limits import Limiter
from server.settings import settings

log = logging.getLogger("loom.router")

# A placement call's body is three short fields: anything larger is refused unread.
MAX_BODY_BYTES = 64 * 1024  # not a setting: the request's own shape bounds it
# How long a readiness answer stands: a probe every second reads the directory once.
READY_TTL_S = 1.0  # not a setting: the probe's own cadence bounds it


def _refused(status: int, reason: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status, headers=headers)


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
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def _body(request: Request) -> Any:
    size = request.headers.get("content-length")
    if size is not None and (not size.isdigit() or int(size) > MAX_BODY_BYTES):
        return None
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
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

    async def place(self, request: Request) -> JSONResponse:
        token = _bearer(request)
        if not token:
            return _refused(401, "the credential names no cell of this directory")
        body = await _body(request)
        if not isinstance(body, dict):
            return _refused(400, "a placement is a JSON object: org, cell, fingerprint")
        org, name = _org(body.get("org")), body.get("cell")
        if org is None or not isinstance(name, str) or not cell_state.NAME.fullmatch(name):
            return _refused(400, "a placement names an org and a cell")
        try:
            expected = await self._credential(name)
        except directory.DirectoryUnavailable as e:
            return _refused(503, e.reason)
        presented = hashlib.sha256(token.encode()).hexdigest()
        if expected is None or not hmac.compare_digest(presented, expected):
            return _refused(401, "the credential names no cell of this directory")
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

    async def lookup(self, request: Request) -> JSONResponse:
        client = request.client.host if request.client else ""
        if not self.lookups.admit(client):
            wait = str(self.lookups.wait_s(client))
            return _refused(429, "too many lookups", {"Retry-After": wait})
        org = _org(request.query_params.get("org"))
        if org is None:
            return _refused(400, "a lookup names an org: ?org=")
        try:
            found = await directory.lookup(org)
            if found is None:
                first = await directory.first_cell()
                found = (first.cell, first.host) if first else None
        except directory.DirectoryUnavailable as e:
            return _refused(503, e.reason)
        if found is None:
            return _refused(503, "the directory holds no cell yet")
        return JSONResponse({"org": org, "cell": found[0], "host": found[1]})

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


def build_app(router: Router | None = None) -> Starlette:
    router = router or Router()

    @asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await directory.close()

    return Starlette(
        routes=[
            Route("/cells/v1/placements", router.place, methods=["POST"]),
            Route("/cells/lookup", router.lookup, methods=["GET"]),
            Route("/livez", router.livez, methods=["GET"]),
            Route("/readyz", router.readyz, methods=["GET"]),
        ],
        lifespan=_lifespan,
    )
