"""HTTP transport with connection pooling, retries, and idempotency keys.

The transport identifies itself (`User-Agent`, `X-Loom-SDK`,
`X-Loom-Runtime`, `X-Loom-Retry-Count`), sends an `X-Request-Id` of the form
the server accepts and echoes (1 to 64 letters, digits, `.`, `_` or `-`),
never exposes the credential through `repr()`, parses `Retry-After` as
seconds or an HTTP-date, and hands back the response metadata (`CallResult`)
when a caller asks for it.
"""

from __future__ import annotations

import asyncio
import contextvars
import platform
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from loom._logging import logger, redact_headers, redact_secret
from loom._version import __version__
from loom.credentials import StoredLogin
from loom.errors import (
    DEFAULT_RETRY_AFTER_S,
    INVALID_RESPONSE_CODE,
    NETWORK_ERROR_CODE,
    BackendUnavailable,
    LoomError,
    RateLimited,
    from_response,
)
from loom.machine import MachineLogin
from loom.retry import DEFAULT_MAX_RETRY_AFTER_S, DEFAULT_RETRY_POLICY, RetryPolicy, coerce_policy
from loom.token import TokenClaims, decode_token_claims

SDK_NAME = "loom-py"
#: Sent as `User-Agent` and `X-Loom-SDK` on every request.
SDK_IDENT = f"{SDK_NAME}/{__version__}"


def runtime_ident() -> str:
    """`python/3.12.3 linux x86_64`: the runtime the server sees in
    `X-Loom-Runtime`; free of anything identifying the host."""
    v = sys.version_info
    return f"python/{v.major}.{v.minor}.{v.micro} {sys.platform} {platform.machine() or 'unknown'}"


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait for a `Retry-After` header: a non-negative number
    (`"2"`, `"0.5"`) or an HTTP-date (RFC 7231 `Wed, 21 Oct 2026 07:28:00
    GMT`), measured from `now` (epoch seconds; default `time.time()`).
    Returns None when the header is absent or unparseable so the caller can
    pick its own delay; a date in the past is 0."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:  # pragma: no cover - older stdlib returned None
            return None
        if when.tzinfo is None:  # `-0000` and the asctime form: HTTP-dates are UTC
            when = when.replace(tzinfo=UTC)
        base = time.time() if now is None else now
        secs = when.timestamp() - base
    if secs != secs or secs in (float("inf"), float("-inf")):
        return None
    return max(0.0, secs)


def retry_hint(resp: httpx.Response, detail: Mapping[str, Any]) -> float | None:
    """The wait a response asks for: its `Retry-After`, else the error
    body's `detail.retry_after_s` (a server that sends only the body field)."""
    raw = resp.headers.get("Retry-After") or detail.get("retry_after_s")
    return None if raw is None else parse_retry_after(str(raw))


def retry_after_from(resp: httpx.Response, detail: Mapping[str, Any] | None = None) -> float:
    """`retry_hint`, bounded: an absent, unparseable or over-cap (60 s) hint
    becomes `DEFAULT_RETRY_AFTER_S`."""
    parsed = retry_hint(resp, detail or {})
    if parsed is None or parsed > DEFAULT_MAX_RETRY_AFTER_S:
        return DEFAULT_RETRY_AFTER_S
    return parsed


TOKEN_FILE = Path("~/.loom/token")


def token_from_file(path: Path | None = None) -> str | None:
    """The token `loom mint-token --save` wrote: the first
    line of `~/.loom/token`, or None when the file is missing, unreadable or
    blank. The last credential source after `api_key=` and `LOOM_API_KEY`;
    never overrides either."""
    target = (path or TOKEN_FILE).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    token = text.strip().splitlines()[0].strip() if text.strip() else ""
    return token or None


#: The server bounds the ledger's `consumer` column to this many characters.
CONSUMER_MAX_CHARS = 64


def consumer_label(value: Any) -> str:
    """The `consumer` label as the server stores it: printable,
    stripped, at most 64 characters; '' for None, a blank, or the wire's
    sentinel `"-"`. Used both for the Transport's own label and to decide
    whether a body already names one."""
    if value is None or isinstance(value, bool):
        return ""
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    if text in ("", "-"):
        return ""
    return text[:CONSUMER_MAX_CHARS]


def _without_userinfo(url: str) -> str:
    """`url` as a repr or an error message may show it: no `user:password@`."""
    return str(httpx.URL(url).copy_with(userinfo=b"")) if "@" in url else url


@dataclass
class RateLimit:
    """Last-known X-RateLimit-* values for a path."""

    limit: int
    remaining: int
    reset_s: int


@dataclass
class CallResult:
    """One successful call with the response metadata the server sends:
    `request_id` finds the call in the server's ledger,
    `schema_version` is the wire schema it answered with, `server_timing_ms`
    its own measure of the handler, `rate_limit` the `X-RateLimit-*` reading
    on this response (None when the endpoint sends none)."""

    data: dict[str, Any]
    status: int
    request_id: str
    schema_version: str
    server_timing_ms: float | None
    rate_limit: RateLimit | None
    attempts: int
    # The store's one-line advice for this call (`X-Loom-Hint`),
    # None when the server sent none. Logged at INFO once per distinct text.
    hint: str | None = None
    hint_code: str = ""


@dataclass
class _Call:
    """The call in flight: its policy, and the HTTP attempts it has sent
    across every 429 pacing round (`X-Loom-Retry-Count`, `.attempts`)."""

    policy: RetryPolicy
    sent: int = 0


# The request ids the server keeps as sent; it replaces any other with its own.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Read by `_post_once` (a contextvar so a per-call override is safe under
# concurrent calls and the `_post_once` signature tests stub stays
# four-positional).
_CALL: contextvars.ContextVar[_Call | None] = contextvars.ContextVar("loom_call", default=None)

# The server never saw these: safe to retry under `retry_connection_errors`.
_CONNECTION_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# The server may still be processing these: retried only when the policy says
# so (by default, only for an op whose Idempotency-Key the server dedups on:
# idempotent and not read-only, so a timed-out `memory.ask` is not run twice).
_TIMEOUT_ERRORS = (httpx.ReadTimeout, httpx.WriteTimeout)
# The server answered but the response broke off: it may have acted.
_CUT_OFF_ERRORS = (httpx.ReadError, httpx.RemoteProtocolError, httpx.DecodingError)
# A retried status re-runs the op: only an idempotent op's Idempotency-Key is
# deduplicated, so any other op is retried only on an answer that proves the
# handler never ran — a 408 (the request never fully arrived) or a refusal
# the server makes before dispatch.
_NEVER_RAN_CODES = frozenset({"AUTH_UNAVAILABLE"})
# 5xx answers to a deployment's configuration, which no retry changes: the
# operator's fix is named in the error (and in /v1/health). A model call that
# names no customer is a server path to fix, not a passing fault.
_CONFIGURATION_CODES = frozenset(
    {"CODED_WIDTH_MISMATCH", "GATEWAY_REQUIRED", "MODEL_CALL_UNATTRIBUTED"}
)


def _never_ran(status: int, code: str) -> bool:
    return status == 408 or code in _NEVER_RAN_CODES


def unreachable_error(
    base_url: str, exc: BaseException, *, request_id: str = ""
) -> BackendUnavailable:
    """`BackendUnavailable` for a request that got no complete response:
    `cannot reach <base_url>: <reason>` (or `response from <base_url> cut
    off: <reason>`), `code="NETWORK"`, `status=0`, `request_id` the id this
    SDK sent. The httpx exception rides along as `__cause__`."""
    base_url = _without_userinfo(base_url)
    detail = str(exc).strip()
    reason = type(exc).__name__ + (f": {detail}" if detail else "")
    where = (
        f"response from {base_url} cut off"
        if isinstance(exc, _CUT_OFF_ERRORS)
        else f"cannot reach {base_url}"
    )
    err = BackendUnavailable(f"{where}: {reason}")
    err.code = NETWORK_ERROR_CODE
    err.status = 0
    err.request_id = request_id
    err.__cause__ = exc
    return err


class Transport:
    """One Transport per Agent. Owns an httpx AsyncClient with HTTP/2 + pool.

    When `pacing=True` (the default), the transport reads
    `X-RateLimit-*` response headers and pre-emptively sleeps one refill tick
    before the next call when the remaining budget for that path is at floor,
    and a 429 that still arrives is retried after the server's `Retry-After`
    until `ratelimit_max_wait_s` of waiting has been spent on that one call
    (each sleep capped at `_MAX_PACING_SLEEP_S`), then surfaces as
    `RateLimited`. A client that fires bursts without reading the hint sees
    a large share of its calls rejected. `pacing=False` restores the
    immediate `RateLimited` for callers that schedule retries themselves.
    """

    # Cap a single pre-flight sleep so a long Reset can't stall a caller forever.
    _MAX_PACING_SLEEP_S = 5.0
    # Total 429 waiting one call may spend before RateLimited surfaces.
    DEFAULT_RATELIMIT_MAX_WAIT_S = 15.0

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        *,
        pacing: bool = True,
        ratelimit_max_wait_s: float | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        consumer: str = "",
        auth: StoredLogin | MachineLogin | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        #: The consumer this process speaks for. Stamped into every body that
        #: names none (the ledger's `consumer` column); '' sends nothing and
        #: leaves every payload unchanged.
        self.consumer: str = consumer_label(consumer)
        #: The retry policy; `post(..., retry=)` overrides per call.
        self.retry: RetryPolicy = coerce_policy(retry, DEFAULT_RETRY_POLICY)
        # A blank or whitespace credential is no credential (an exported but
        # empty LOOM_API_KEY must not send `Bearer ` and confuse the 401).
        api_key = (api_key or "").strip() or None
        # `auth` (a `loom login` profile) sends and refreshes its own token.
        self._auth = auth
        if auth is not None:
            self._key_hint = f"login:{auth.profile}"
        else:
            self._key_hint = redact_secret(api_key) if api_key else "none"
        # The credential lives in the httpx client's header set only; this
        # object's attributes and repr never carry it.
        self._hints_seen: set[str] = set()
        self._ident_headers: dict[str, str] = {
            "User-Agent": SDK_IDENT,
            "X-Loom-SDK": SDK_IDENT,
            "X-Loom-Runtime": runtime_ident(),
        }
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            http2=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            auth=auth,
        )
        self._timeout_s = float(timeout_s)
        self._pacing = pacing
        self._ratelimit_max_wait_s = float(
            self.DEFAULT_RATELIMIT_MAX_WAIT_S
            if ratelimit_max_wait_s is None
            else max(0.0, ratelimit_max_wait_s)
        )
        self._rate_state: dict[str, RateLimit] = {}
        # An optional `async (op_path, payload) -> headers`
        # hook run before every POST; the ambient run uses it to stamp writes
        # and to label reads with `X-Loom-Run-Id`. None = no hook.
        self.before_post: Callable[[str, dict[str, Any]], Awaitable[dict[str, str]]] | None = None

    @property
    def pacing(self) -> bool:
        return self._pacing

    def with_consumer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The body as it goes out: the Transport's `consumer`
        added when one is set and the body names none (absent, None, '' or
        `"-"`). A body that names its own consumer wins; with no Transport
        consumer the payload is returned untouched, so a caller that sets
        nothing sends byte-for-byte what it sent before."""
        if not self.consumer or consumer_label(payload.get("consumer")):
            return payload
        return {**payload, "consumer": self.consumer}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_credential(self) -> bool:
        """Whether an `Authorization` header goes out with every request."""
        return self._auth is not None or "Authorization" in self._client.headers

    def token_claims(self) -> TokenClaims | None:
        """The bearer token's payload (org, namespaces, roles, ns_roles),
        decoded, not verified; None without a JWT."""
        if self._auth is not None:
            return decode_token_claims(self._auth.current_token() or None)
        auth = self._client.headers.get("Authorization", "")
        return decode_token_claims(auth[7:] if auth.startswith("Bearer ") else None)

    def rate_limit(self, op_path: str) -> RateLimit | None:
        """The last `X-RateLimit-*` reading for an op path, if any."""
        return self._rate_state.get(op_path)

    def __repr__(self) -> str:
        return (
            f"Transport(base_url={_without_userinfo(self._base_url)!r}, api_key={self._key_hint}, "
            f"timeout_s={self._timeout_s:g}, pacing={self._pacing}, "
            f"retry={self.retry.max_attempts} attempts, sdk={SDK_IDENT!r})"
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _record_rate_state(self, op_path: str, resp: httpx.Response) -> None:
        try:
            limit = int(resp.headers["X-RateLimit-Limit"])
            remaining = int(resp.headers["X-RateLimit-Remaining"])
            reset_s = int(resp.headers["X-RateLimit-Reset"])
        except (KeyError, ValueError):
            return  # endpoint didn't emit rate-limit headers
        self._rate_state[op_path] = RateLimit(limit, remaining, reset_s)

    def _pace_sleep_s(self, op_path: str) -> float:
        """Return how long to sleep before the next request on this path."""
        state = self._rate_state.get(op_path)
        if state is None or state.remaining >= 1:
            return 0.0
        # One refill tick: reset_s / limit. e.g. limit=10, reset=5 → 0.5s.
        per_token = max(0.0, state.reset_s / max(state.limit, 1))
        return min(per_token, self._MAX_PACING_SLEEP_S)

    async def post(
        self,
        op_path: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        timeout_s: float | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST to /v1/{op} and return the decoded body; see `post_with_meta`."""
        result = await self.post_with_meta(
            op_path, payload, idempotency_key, timeout_s=timeout_s, retry=retry
        )
        return result.data

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """One request to a route outside the op registry (`/v1/me/...`,
        `/v1/admin/...`, `/v1/health`): the body goes as given, with no
        identity or consumer added, and is not retried. Raises the typed
        error for a 4xx/5xx; returns the response for the caller to read.
        `path` is a path on this server: the credential goes nowhere else."""
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"not a path on {self.base_url}: {path!r}")
        request_id = str(uuid.uuid4())
        headers = {**self._ident_headers, "X-Request-Id": request_id}
        try:
            resp = await self._client.request(
                method, path, json=json, params=params, headers=headers
            )
        except httpx.RequestError as e:
            raise unreachable_error(self.base_url, e, request_id=request_id) from e
        if resp.status_code >= 400:
            raise self._error_for(path, resp, resp.headers.get("X-Request-Id") or request_id)
        return resp

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`request`, decoded: the JSON object the route answers."""
        resp = await self.request(method, path, json=json, params=params)
        return _json_object(path, resp, resp.headers.get("X-Request-Id", ""))

    async def post_with_meta(
        self,
        op_path: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        timeout_s: float | None = None,
        request_id: str | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> CallResult:
        """POST to /v1/{op} with retries on 5xx/429; return body and metadata.

        Args:
            op_path: Operation path appended to the base URL.
            payload: JSON request body.
            idempotency_key: Sent as the Idempotency-Key header; when omitted,
                a UUID is generated for an op the server replays (any but a
                read-only one).
            timeout_s: Per-call override of the Transport's default timeout,
                for ops that legitimately run longer than the default.
            request_id: Sent as `X-Request-Id` when it is 1 to 64 letters,
                digits, `.`, `_` or `-`, the form the server keeps; a UUID
                is sent instead when it is omitted or of another form. The
                server echoes the id, the log lines name it, and every
                `LoomError` this call raises carries it as `.request_id`.
            retry: A `RetryPolicy` or a dict of its fields overriding the
                Transport's policy for this call (`{"max_attempts": 1}`
                disables retries).
            extra_headers: More request headers for this call, for example
                `X-Loom-Run-Id`, which labels a read with a run.

        Returns:
            A `CallResult`: the decoded JSON body plus the request id, the
            server's schema version, its timing and rate-limit reading.

        Raises:
            RateLimited: On a 429 past the pacing budget (see `Transport`).
            LoomError: Any other failure, after the policy's retries.
        """
        headers: dict[str, str] = dict(extra_headers or {})
        if idempotency_key is None and _replayed(op_path):
            idempotency_key = str(uuid.uuid4())
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        headers["X-Request-Id"] = (
            request_id if request_id and _REQUEST_ID.fullmatch(request_id) else str(uuid.uuid4())
        )
        if request_id and headers["X-Request-Id"] != request_id:
            logger.warning(
                "%s request_id %r is not 1-64 letters, digits, '.', '_' or '-'; sent %s",
                op_path,
                request_id,
                headers["X-Request-Id"],
            )
        payload = self.with_consumer(payload)
        if self.before_post is not None:
            extra = await self.before_post(op_path, payload)
            if extra:
                headers.update(extra)
        # A RunScope injects its run_id into every body; on a recall read that is
        # a filter that empties the result, so move it to the label header
        # (works with or without an ambient run). Import here to avoid a cycle.
        from loom.run import scope_read_label

        for _k, _v in scope_read_label(op_path, payload).items():
            headers.setdefault(_k, _v)

        if self._pacing:
            wait = self._pace_sleep_s(op_path)
            if wait > 0:
                await asyncio.sleep(wait)

        call = _Call(coerce_policy(retry, self.retry))
        started = time.monotonic()
        waited_s = 0.0
        while True:
            token = _CALL.set(call)
            try:
                out = await self._post_once(op_path, payload, headers, timeout_s)
            except RateLimited as e:
                if not self._pacing:
                    logger.error(
                        "%s 429 rate limited; pacing off (rid=%s)", op_path, headers["X-Request-Id"]
                    )
                    raise
                # Honor the server hint; every sleep is capped and the total
                # is bounded, so a glacial refill surfaces instead of stalling.
                budget_left = self._ratelimit_max_wait_s - waited_s
                if budget_left <= 0:
                    logger.error(
                        "%s 429 rate limited; %.1f s pacing budget spent (rid=%s)",
                        op_path,
                        self._ratelimit_max_wait_s,
                        headers["X-Request-Id"],
                    )
                    raise
                wait = min(
                    max(float(e.retry_after_s), 0.0),
                    self._MAX_PACING_SLEEP_S,
                    budget_left,
                )
                if wait <= 0:
                    raise
                logger.warning(
                    "%s 429 rate limited; waiting %.2f s (rid=%s)",
                    op_path,
                    wait,
                    headers["X-Request-Id"],
                )
                await asyncio.sleep(wait)
                waited_s += wait
            except LoomError as e:
                logger.error(
                    "%s failed after %.0f ms: %s %s (rid=%s)",
                    op_path,
                    (time.monotonic() - started) * 1000,
                    type(e).__name__,
                    e.code,
                    headers["X-Request-Id"],
                )
                raise
            else:
                # Tests and subclasses may stub `_post_once` with a bare body.
                if isinstance(out, CallResult):
                    return out
                return CallResult(
                    data=out,
                    status=200,
                    request_id=headers["X-Request-Id"],
                    schema_version="",
                    server_timing_ms=None,
                    rate_limit=None,
                    attempts=1,
                )
            finally:
                _CALL.reset(token)

    async def _post_once(
        self,
        op_path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_s: float | None = None,
    ) -> Any:
        """The attempt loop for one pacing round of the call `post_with_meta`
        set in `_CALL` (what is retried: `loom.retry`). A 429 raises
        `RateLimited` for the pacing loop in `post_with_meta`."""
        call = _CALL.get() or _Call(self.retry)
        policy = call.policy
        request_kwargs: dict[str, Any] = {}
        if timeout_s is not None:
            request_kwargs["timeout"] = httpx.Timeout(timeout_s)
        request_id = headers.get("X-Request-Id", "")
        idempotent = _op_is_idempotent(op_path)
        deduplicated = idempotent and _replayed(op_path)
        slept_s = 0.0
        attempt = 0
        while True:
            attempt += 1
            call.sent += 1
            sent = {**self._ident_headers, **headers, "X-Loom-Retry-Count": str(call.sent - 1)}
            logger.debug(
                "%s attempt %d/%d timeout %s headers %s",
                op_path,
                attempt,
                policy.max_attempts,
                timeout_s if timeout_s is not None else self._timeout_s,
                redact_headers({**dict(self._client.headers), **sent}),
            )
            t0 = time.monotonic()
            try:
                resp = await self._client.post(
                    op_path, json=payload, headers=sent, **request_kwargs
                )
            except _CONNECTION_ERRORS as e:
                slept_s += await self._retry_or_raise(
                    op_path,
                    policy,
                    attempt,
                    slept_s,
                    allowed=policy.retry_connection_errors,
                    why=f"connection failed: {type(e).__name__}",
                    request_id=request_id,
                    error=unreachable_error(self.base_url, e, request_id=request_id),
                )
                continue
            except BackendUnavailable as e:
                # The token provider (the auth flow) could not get a token: the
                # op did not run (at most a 401 came back before a fresh token
                # was asked for), so it is retried as a connection failure.
                call.sent -= 1
                slept_s += await self._retry_or_raise(
                    op_path,
                    policy,
                    attempt,
                    slept_s,
                    allowed=policy.retry_connection_errors,
                    why=f"token unavailable: {e}",
                    request_id=request_id,
                    error=e,
                )
                continue
            except _TIMEOUT_ERRORS as e:
                slept_s += await self._retry_or_raise(
                    op_path,
                    policy,
                    attempt,
                    slept_s,
                    allowed=policy.retries_timeout(idempotent=deduplicated),
                    why=f"timed out ({type(e).__name__}); idempotent",
                    request_id=request_id,
                    error=unreachable_error(self.base_url, e, request_id=request_id),
                )
                continue
            except httpx.RequestError as e:
                # Any other failure (a response cut off, a protocol or proxy
                # error): typed, and not retried — the server may have acted.
                raise unreachable_error(self.base_url, e, request_id=request_id) from e
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._record_rate_state(op_path, resp)
            echoed = resp.headers.get("X-Request-Id") or request_id
            if resp.status_code >= 400:
                err = self._error_for(op_path, resp, echoed)
                allowed = (
                    resp.status_code != 429
                    and policy.retries_status(resp.status_code)
                    and err.code not in _CONFIGURATION_CODES
                    and (idempotent or _never_ran(resp.status_code, err.code))
                )
                delay = self._retry_delay(
                    op_path,
                    policy,
                    attempt,
                    slept_s,
                    allowed=allowed,
                    hint=retry_hint(resp, err.detail),
                )
                if delay is None:
                    raise err
                why = f"{resp.status_code} {err.code}"
                slept_s += await self._backoff(op_path, policy, attempt, delay, why, echoed)
                continue
            return self._result(op_path, resp, echoed, call.sent, elapsed_ms)

    def _result(
        self, op_path: str, resp: httpx.Response, echoed: str, attempt: int, elapsed_ms: float
    ) -> CallResult:
        """The `CallResult` of a 2xx response, its hint logged once per text."""
        body = _json_object(op_path, resp, echoed)
        logger.info(
            "%s %d %.0f ms attempts=%d rid=%s",
            op_path,
            resp.status_code,
            elapsed_ms,
            attempt,
            echoed,
        )
        hint = resp.headers.get("X-Loom-Hint") or None
        if hint and hint not in self._hints_seen:
            # The store mentors the agent; a bot with no
            # pack to render still sees the advice in its own log.
            self._hints_seen.add(hint)
            logger.info(
                "loom hint (%s) on %s: %s",
                resp.headers.get("X-Loom-Hint-Code", ""),
                op_path,
                hint,
            )
        return CallResult(
            data=body,
            status=resp.status_code,
            request_id=echoed,
            schema_version=resp.headers.get("X-Schema-Version", ""),
            server_timing_ms=app_duration_ms(resp.headers.get("Server-Timing")),
            rate_limit=self._rate_state.get(op_path),
            attempts=attempt,
            hint=hint,
            hint_code=resp.headers.get("X-Loom-Hint-Code", ""),
        )

    @staticmethod
    def _retry_delay(
        op_path: str,
        policy: RetryPolicy,
        attempt: int,
        slept_s: float,
        *,
        allowed: bool,
        hint: float | None = None,
    ) -> float | None:
        """Seconds to sleep before the next attempt, or None to stop: the
        policy forbids this kind of failure, the attempts are spent, or the
        delay would overrun the total backoff budget. The server's `hint`
        (`retry_hint`) replaces the backoff when the policy respects it and it
        is within `max_retry_after_s`."""
        if not allowed or attempt >= policy.max_attempts:
            return None
        delay = policy.backoff_for(attempt)
        if hint is not None and policy.respect_retry_after and hint <= policy.max_retry_after_s:
            delay = hint
        if slept_s + delay > policy.total_backoff_budget_s:
            logger.warning(
                "%s: backoff budget of %.1f s spent; giving up",
                op_path,
                policy.total_backoff_budget_s,
            )
            return None
        return delay

    async def _retry_or_raise(
        self,
        op_path: str,
        policy: RetryPolicy,
        attempt: int,
        slept_s: float,
        *,
        allowed: bool,
        why: str,
        request_id: str,
        error: BaseException,
    ) -> float:
        """Sleep before the next attempt of a request that got no response and
        return the seconds slept; raise `error` when the policy stops
        (`_retry_delay`). `error` keeps its own `__cause__`: the httpx error
        `unreachable_error` attaches, or what the token provider raised from."""
        delay = self._retry_delay(op_path, policy, attempt, slept_s, allowed=allowed)
        if delay is None:
            raise error
        return await self._backoff(op_path, policy, attempt, delay, why, request_id)

    @staticmethod
    async def _backoff(
        op_path: str, policy: RetryPolicy, attempt: int, delay: float, why: str, request_id: str
    ) -> float:
        """Log the retry and sleep `delay`; returns it for the budget."""
        logger.warning(
            "%s attempt %d/%d %s; retrying in %.2f s (rid=%s)",
            op_path,
            attempt,
            policy.max_attempts,
            why,
            delay,
            request_id,
        )
        await asyncio.sleep(delay)
        return delay

    @staticmethod
    def _error_for(op_path: str, resp: httpx.Response, request_id: str) -> LoomError:
        """The exception for a non-2xx response (`from_response`), with the
        request id and the server's bounded retry hint."""
        try:
            body: object = resp.json()
        except ValueError:  # a text or HTML error page
            body = resp.text
        err = from_response(body, resp.status_code, request_id=request_id)
        if hasattr(err, "retry_after_s"):
            err.retry_after_s = retry_after_from(resp, err.detail)
        return err


def _json_object(op_path: str, resp: httpx.Response, request_id: str) -> dict[str, Any]:
    """A 2xx body: `{}` when empty, else it must be a JSON object; anything
    else is `LoomError(code=INVALID_RESPONSE)` quoting its first 200
    characters."""
    if not resp.content:
        return {}
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    err = LoomError(
        f"{op_path}: HTTP {resp.status_code} response is not a JSON object "
        f"({len(resp.content)} bytes): {resp.text[:200]!r}"
    )
    err.code = INVALID_RESPONSE_CODE
    err.status = resp.status_code
    err.request_id = request_id
    err.detail = {"message": resp.text[:200]}
    raise err


def _replayed(op_path: str) -> bool:
    """Whether the server keeps a replay of the op behind `/v1/<op>`: any but
    a read-only one (an op this SDK does not know included)."""
    from loom.ops import spec  # generated; imported lazily to keep import time low

    s = spec(op_path.rsplit("/", 1)[-1])
    return s is None or not s.read_only


def _op_is_idempotent(op_path: str) -> bool:
    """Whether the registry marks the op behind `/v1/<op>` idempotent."""
    from loom.ops import spec  # generated; imported lazily to keep import time low

    name = op_path.rsplit("/", 1)[-1]
    s = spec(name)
    return bool(s is not None and s.idempotent)


def app_duration_ms(server_timing: str | None) -> float | None:
    """The `dur` of the `app` metric in a `Server-Timing` header (the
    server's own measure of the handler), or None when absent."""
    for metric in (server_timing or "").split(","):
        name, *params = (p.strip() for p in metric.split(";"))
        if name != "app":
            continue
        for param in params:
            key, _, value = param.partition("=")
            if key.strip() == "dur":
                try:
                    return float(value)
                except ValueError:
                    return None
    return None
