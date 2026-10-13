"""Server-side error taxonomy.

Mirrors the SDK's exception hierarchy at `sdk/python/loom/errors.py`: every
server `code` is a class the SDK deserializes via `errors.from_response`.
"""

from __future__ import annotations

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
    """409 — `if_match_sha256` mismatch. Returns the live hash so the caller
    can retry without a separate read."""

    code = "PRECONDITION_FAILED"
    status_code = 409

    def __init__(self, message: str = "", *, current_sha256: str = "") -> None:
        super().__init__(message, current_sha256=current_sha256)


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


class OverloadedError(LoomServerError):
    """503 — every pooled ClickHouse connection of this process was busy for
    longer than `ch_pool_wait_ms`. The request is refused
    before it queues; `retry_after_s` is the hint the SDK's retry layer honours."""

    code = "OVERLOADED"
    status_code = 503

    def __init__(self, message: str = "", *, retry_after_s: int = 1) -> None:
        super().__init__(message, retry_after_s=retry_after_s)


class RateLimitedError(LoomServerError):
    """429 — the caller is over one of its admission limits (the org's in-flight
    requests); `retry_after_s` is the hint the SDK's retry layer honours."""

    code = "RATE_LIMITED"
    status_code = 429

    def __init__(self, message: str = "", *, retry_after_s: int = 1) -> None:
        super().__init__(message, retry_after_s=retry_after_s)


class BackendUnavailableError(LoomServerError):
    """503 — ClickHouse could not be reached after the bounded reconnect.

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


class EmbedTimeoutError(LoomServerError):
    """503 — the query embedding did not answer within
    `memory_search_embed_bound_s` on any attempt. The search wrote nothing;
    an SDK caller has already retried it."""

    code = "EMBED_TIMEOUT"
    status_code = 503


class OverrideNotSharedError(LoomServerError):
    """503 — a runtime override (budget or rate limit) could not be shared with
    the server's other web processes, so none of them took it. No
    `Retry-After`: the fix is the runtime directory's (permissions, space);
    the server log names the file."""

    code = "OVERRIDE_NOT_SHARED"
    status_code = 503


class CodedWidthMismatchError(LoomServerError):
    """503 — a vector in the write is not the width its `Quantized` column was
    created at, so ClickHouse would refuse the insert. Refused before any row of
    the write lands; `/v1/health` reports `coded_width_mismatch` with the fix."""

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


def register_exception_handler(app: FastAPI) -> None:
    """Turn any LoomServerError into `{"detail": {"code", "message", ...}}`, and give every
    coded HTTP error a `Link: <catalogue#code>; rel="help"` header."""
    from fastapi import Request
    from fastapi.exception_handlers import http_exception_handler
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
