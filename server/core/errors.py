"""Server-side error taxonomy.

Mirrors the SDK's exception hierarchy at `sdk/python/loom/errors.py`: every
server `code` is a class the SDK deserializes via `errors.from_response`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


class LoomServerError(Exception):
    """Base class. Subclasses set `code` (matches the SDK's `ERROR_CODES`)
    and `status_code`."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra: dict[str, Any] = extra

    def to_detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        d.update(self.extra)
        return d

    def headers(self) -> dict[str, str] | None:
        """The retry hint on the standard header too, for any client or proxy."""
        hint = self.extra.get("retry_after_s")
        return {"Retry-After": str(hint)} if hint is not None else None


class ValidationError(LoomServerError):
    code = "VALIDATION_FAILED"
    status_code = 422


class AuthInsufficientError(LoomServerError, PermissionError):
    """A public 403 that remains catchable as ``PermissionError`` internally.

    Domain callers handle authorization failures as ``PermissionError``
    while the transport receives the stable structured error code.
    """

    code = "AUTH_INSUFFICIENT"
    status_code = 403


class IdentityIncompleteError(LoomServerError):
    """A scoped token was presented without org+namespace in the request
    body. NOT a permissions failure — the token may be perfectly
    authorized; the request just didn't say for which namespace. Distinct
    from AUTH_INSUFFICIENT (403) so callers stop chasing their token when
    the real fix is 'put org/namespace in the body'."""

    code = "IDENTITY_INCOMPLETE"
    status_code = 400


class NotFoundError(LoomServerError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404


class PreconditionFailedError(LoomServerError):
    """409 — `if_match_sha256` mismatch, or an erasure that landed while a
    `namespace.restore` ran. A mismatch returns the live hash so the caller can retry
    without a separate read."""

    code = "PRECONDITION_FAILED"
    status_code = 409

    def __init__(self, message: str = "", *, current_sha256: str = "") -> None:
        super().__init__(message, current_sha256=current_sha256)


class SourceReplacedError(LoomServerError):
    """409 — the document source was removed and added again as another source
    during the call (a rotate's probe, a sync request); act on the current one."""

    code = "SOURCE_REPLACED"
    status_code = 409


class ReadsLlmFreeError(LoomServerError):
    """409 — `memory.ask` or `code.ask` asked for a model's answer while
    `LOOM_READS_LLM_FREE` is on. Resending the same request never succeeds;
    the retrieval-only flag the message names returns the recalled evidence."""

    code = "READS_LLM_FREE"
    status_code = 409


class GrantWouldDowngradeError(LoomServerError):
    """409 — `grants.create` asked for a role below the subject's current
    active grant on that namespace. The grants table is a
    ReplacingMergeTree keyed on (org, namespace, subject), so the insert would
    silently replace an admin with a viewer. Carries both roles; the caller
    repeats the call with `replace=true` to lower the grant on purpose."""

    code = "GRANT_WOULD_DOWNGRADE"
    status_code = 409

    def __init__(
        self, message: str = "", *, current_role: str = "", requested_role: str = ""
    ) -> None:
        super().__init__(message, current_role=current_role, requested_role=requested_role)


class GrantBusyError(LoomServerError):
    """503 — another change to the same grant held it past the wait. Retry
    after `retry_after_s`."""

    code = "GRANT_BUSY"
    status_code = 503

    def __init__(self, message: str = "", *, retry_after_s: int = 1) -> None:
        super().__init__(message, retry_after_s=retry_after_s)


class OverloadedError(LoomServerError):
    """503 — too busy, retry: the ClickHouse pool or its streams, the document
    bucket's transfers or streams, an override's lock, a namespace's document-source
    lease, the blacklist's regex matchers, or a conversation delete or compaction stayed
    contended. The pool: every pooled connection was busy past `ch_pool_wait_ms`; the
    lock or lease: a sibling held it past its wait; the matchers: the org's share of them
    stayed busy, and the write is refused rather than stored unchecked; a conversation
    delete or compaction: writes to the thread's embeddings kept landing. A retry is
    safe; `retry_after_s` is the hint the SDK's retry layer honours."""

    code = "OVERLOADED"
    status_code = 503

    def __init__(self, message: str = "", *, retry_after_s: int = 1) -> None:
        super().__init__(message, retry_after_s=retry_after_s)


class OverrideBusyError(OverloadedError):
    """OVERLOADED because another web process was updating the same runtime
    override; the admin route waits it out, and nothing changed."""


class RateLimitedError(LoomServerError):
    """429 — the caller is over one of its admission limits (the org's in-flight
    requests); `retry_after_s` is the hint the SDK's retry layer honours."""

    code = "RATE_LIMITED"
    status_code = 429

    def __init__(self, message: str = "", *, retry_after_s: int = 1) -> None:
        super().__init__(message, retry_after_s=retry_after_s)


class GatewayRequiredError(LoomServerError):
    """503 — a model call named a host LOOM_INFERENCE_GATEWAY_HOSTS does not
    list while the gateway is required (`LOOM_INFERENCE_GATEWAY_REQUIRED`, or a
    workload token); the message names the host. Also every data-plane request
    on a server whose saved model configuration the check refuses, until a
    replacement is saved or the configuration is reset; that answer names no
    host. A setting to fix, not an outage: never retried. A classifier or
    reranker refused the same way fails open instead: the caller's type, or
    the input order, stays."""

    code = "GATEWAY_REQUIRED"
    status_code = 503


class ModelCallRefused(LoomServerError):
    """403 — the inference gateway refused the call for the identity it was made
    for: that identity's refusal, not the provider's failure. The answer is one
    `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lists under `refused` (an agent over
    its budget, an agent not allowed). It is not retried, marks no provider
    failing and switches no org off; a background job leaves that identity's
    calls to the same model for `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS`.
    `gateway_status` and `gateway_code` say what the gateway answered."""

    code = "MODEL_CALL_REFUSED"
    status_code = 403

    def __init__(self, message: str = "", *, hold_s: float = 0.0, **extra: Any) -> None:
        super().__init__(message, **extra)
        # How long a job leaves the identity's calls to that route and model (0: none
        # held); not part of the answer.
        self.hold_s = hold_s


class ModelCallDeferred(ModelCallRefused):
    """503 — the inference gateway asked the call's identity to wait for
    `retry_after_s`, its `Retry-After`. The answer is one
    `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lists under `wait` (too many of the
    agent's requests in progress). A request's call asked to wait two seconds or
    less waits once and asks again first; a background job holds that identity's
    calls to the same model as long. The SDKs raise it as `BackendUnavailable`,
    the wait in `detail.retry_after_s` and the `Retry-After` header."""

    code = "MODEL_CALL_DEFERRED"
    status_code = 503


class BackendUnavailableError(LoomServerError):
    """503 — ClickHouse could not be reached after the bounded reconnect, or
    the document bucket failed a read, write or delete.

    The public message deliberately excludes the driver URL and credentials;
    operators get the concrete connection error from health and server logs.
    """

    code = "BACKEND_UNAVAILABLE"
    status_code = 503

    def __init__(
        self, message: str = "", *, operation: str = "", outcome_unknown: bool = False
    ) -> None:
        super().__init__(
            message,
            operation=operation,
            outcome_unknown=outcome_unknown,
            retry_after_s=1,
        )


class BackendTimeoutError(LoomServerError):
    """504 — a ClickHouse statement ran past its class deadline (reads 30 s,
    writes 60 s, DDL 300 s). The slot is released and the
    statement is not retried; `detail` names the class and the bound."""

    code = "BACKEND_TIMEOUT"
    status_code = 504

    def __init__(
        self, message: str = "", *, statement_class: str = "", timeout_s: float = 0.0
    ) -> None:
        super().__init__(message, statement_class=statement_class, timeout_s=timeout_s)


# A read the server could not make now (down, past its deadline, out of connections):
# answered 503/504 so a caller retries, never a refusal or an empty answer.
BACKEND_NOT_NOW = (BackendUnavailableError, BackendTimeoutError, OverloadedError)


class EmbedTimeoutError(LoomServerError):
    """503 — the query embedding did not answer within
    `memory_search_embed_bound_s` on any attempt. The search wrote nothing;
    an SDK caller has already retried it."""

    code = "EMBED_TIMEOUT"
    status_code = 503


class CodedWidthMismatchError(LoomServerError):
    """503 — a vector in the write is not the width its `Quantized` column was
    created at, so ClickHouse would refuse the insert. `memory.set`, `bulk_set`,
    `update` / `update_by_id` and the `knowledge.*` writes are refused before any
    of their rows lands, every other write at the insert that carries the vector;
    `/v1/health` reports `coded_width_mismatch`."""

    code = "CODED_WIDTH_MISMATCH"
    status_code = 503

    def __init__(
        self, message: str = "", *, column: str = "", stored: int = 0, written: int = 0
    ) -> None:
        super().__init__(message, column=column, stored=stored, written=written)


class SchemaPendingError(LoomServerError):
    """503 — the statement names a table that a migration this build carries
    but the service has not applied would create. Refused
    before it reaches ClickHouse, so a leg that treats a failed read as
    additive continues and the ledger row says which table it went without;
    a statement with no such fallback fails the request with the table and
    the migration named."""

    code = "SCHEMA_PENDING"
    status_code = 503

    def __init__(self, message: str = "", *, table: str = "", migration: str = "") -> None:
        super().__init__(message, table=table, migration=migration)


# Each code's entry in the error catalogue; an HTTP error names it in a `Link` header.
ERROR_CATALOGUE_URL = "https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md"


def error_help_link(code: str) -> str:
    """The RFC 8288 `Link` value pointing a client at `code`'s catalogue entry."""
    return f'<{ERROR_CATALOGUE_URL}#{code.lower()}>; rel="help"'


def _with_help_link(headers: dict[str, str] | None, detail: object) -> dict[str, str] | None:
    code = detail.get("code") if isinstance(detail, dict) else None
    if not isinstance(code, str) or not code:
        return headers
    return {**(headers or {}), "Link": error_help_link(code)}


def loads_keeping_non_finite(text: str) -> Any:
    """JSON text parsed with any non-finite number (`Infinity`, `NaN`) kept as a
    string, so the result serialises again: a 422 echoes what a client sent."""
    return json.loads(text, parse_constant=str)


def register_exception_handler(app: FastAPI) -> None:
    """Turn any LoomServerError into `{"detail": {"code", "message", ...}}`, give every
    coded HTTP error a `Link: <catalogue#code>; rel="help"` header, and keep a
    request body's 422 JSON-safe."""
    from fastapi import Request
    from fastapi.encoders import jsonable_encoder
    from fastapi.exception_handlers import http_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(LoomServerError)
    async def _handle_loom_error(_: Request, exc: LoomServerError) -> JSONResponse:
        detail = exc.to_detail()
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=_with_help_link(exc.headers(), detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        headers = _with_help_link(getattr(exc, "headers", None), exc.detail)
        if headers is not getattr(exc, "headers", None):
            exc.headers = headers
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422, except that a non-finite input (`1e400`) is echoed as a
        # string: JSON has no inf, and the response would fail as a 500.
        errors = loads_keeping_non_finite(json.dumps(jsonable_encoder(exc.errors())))
        return JSONResponse(status_code=422, content={"detail": errors})
