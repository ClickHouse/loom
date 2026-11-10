"""Server-side error taxonomy.

Mirrors the SDK's exception hierarchy at `sdk/python/loom/errors.py`: every
server `code` is a class the SDK deserializes via `errors.from_response`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Literal, get_args

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


class CollectionNameTakenError(LoomServerError):
    """409 — no name could be had: the one asked for is used or was a deleted
    collection's (`deleted`), or every name a title derives is taken; `suggestion` is
    a free name when one was found, else ''."""

    code = "COLLECTION_NAME_TAKEN"
    status_code = 409

    def __init__(self, message: str = "", *, deleted: bool = False, suggestion: str = "") -> None:
        super().__init__(message, deleted=deleted, suggestion=suggestion)


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
    """503 — too busy, retry: the ClickHouse pool or its streams, ClickHouse's shared
    memory, the document bucket's transfers or streams, an override's lock, a
    namespace's document-source lease, the blacklist's regex matchers, or a conversation
    delete or compaction stayed contended. The pool: every pooled connection was busy past
    `ch_pool_wait_ms`; ClickHouse's memory: a statement found the server's or the
    ClickHouse user's memory, which every query shares, used up; the lock or lease: a
    sibling held it past its wait; the matchers: the org's share of them stayed busy, and
    the write is refused rather than stored unchecked; a conversation delete or
    compaction: writes to the thread's embeddings kept landing; insights.query while its
    replica's probe has no answer from ClickHouse. Safe to resend on an idempotent op, or
    when it came before the handler ran; `retry_after_s` is the wait."""

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


def caller_text(e: BaseException) -> str:
    """An error as a caller-facing field shows it: a Loom error's code and message,
    which are written for callers; any other error's kind only, since its text can
    carry a statement, a server address or a remote response. The caller logs the
    text."""
    if isinstance(e, LoomServerError):
        return f"{e.code}: {e.message}" if e.message else e.code
    return type(e).__name__


def error_kind(e: BaseException) -> str:
    """An error as a surface without a token shows it: a Loom error's code, any other
    error's class. Even a Loom error's message can name a host (a gateway refusal
    names the one it refused)."""
    return e.code if isinstance(e, LoomServerError) else type(e).__name__


# When each (place, kind) was last logged by `shown_unauthenticated`.
_logged_at: dict[tuple[str, str], float] = {}


def shown_unauthenticated(log: logging.Logger, place: str, e: BaseException) -> str:
    """`error_kind(e)` for a surface any caller can poll without a token, with the
    text logged at most once a minute per place and kind, however often it is polled."""
    kind = error_kind(e)
    now = time.monotonic()
    if now - _logged_at.get((place, kind), float("-inf")) >= 60.0:
        _logged_at[(place, kind)] = now
        log.warning("%s failed: %s: %s", place, type(e).__name__, e)
    return kind


# The wire codes of the `LoomServerError` classes, as `caller_text` leads with them.
_LOOM_CODES: set[str] = set()


def _is_loom_code(word: str) -> bool:
    """`word` is the code of a `LoomServerError` class loaded (`BACKEND_UNAVAILABLE`).
    A known code is a set lookup; an upper-case word not in the set walks the classes
    again, since a module imported later can add one (`ERROR` from a driver walks
    each time, and is not one)."""
    if word in _LOOM_CODES or not word.isupper():
        return word in _LOOM_CODES
    todo: list[type[LoomServerError]] = [LoomServerError]
    while todo:
        cls = todo.pop()
        _LOOM_CODES.add(cls.code)
        todo.extend(cls.__subclasses__())
    return word in _LOOM_CODES


def stored_caller_text(text: str) -> str:
    """An error a row stored, cut to `caller_text`'s shape: `<CODE>: <message>` is kept
    whole when `<CODE>` is a Loom error's (a driver's `ERROR: ...` is not),
    `<Kind>: <text>` (what a build before `caller_text` stored) keeps the kind, and
    text in neither shape reads as `""`. For a field read back from a table that
    an older build may have written. A bare identifier is kept whatever its case
    (`ConnectTimeout`, `gaierror`): one word carries no statement or address. Before
    `: <text>`, a kind is a class name that ends in `Error` or `Exception`, so a
    driver's own lead word (`Code: 241. ...`) is not one."""
    kind, sep, rest = text.partition(": ")
    if rest and _is_loom_code(kind):
        return text
    if not kind.isidentifier():
        return ""
    if not sep:
        return kind
    if kind[0].isupper() and kind.endswith(("Error", "Exception")):
        return kind
    return ""


class AnalysisUnavailableError(LoomServerError):
    """`insights.query` cannot run on this replica: the probe each process
    runs on an interval found, or a statement's login met, the analyst
    ClickHouse user missing, wider than the allowlist, or refusing this
    replica's key or password. Only an operator's fix ends
    it, so it is not retried: `loom doctor` names the cause and the fix. A
    replica whose probe has no answer from ClickHouse, or a ClickHouse short
    of memory, answers OVERLOADED instead."""

    code = "ANALYSIS_UNAVAILABLE"
    status_code = 503


class AssistantUnavailableError(LoomServerError):
    """The assistant cannot answer in this web process: no LibreChat is configured
    (`LOOM_ASSISTANT_LIBRECHAT_URL`), auth is off (an answer is delegated from its
    caller's token), or this process's setup has not succeeded yet (at boot, or again
    after LibreChat refused the stored key). `detail.reason` says which; `/v1/health`
    (`assistant_service_failing`) names a setup try that failed."""

    code = "ASSISTANT_UNAVAILABLE"
    status_code = 503


class AssistantSelfCallError(LoomServerError):
    """An assistant answer's own token, or a token delegated from one, asked the
    assistant: the assistant never calls itself (docs/architecture/assistant.md)."""

    code = "ASSISTANT_SELF_CALL"
    status_code = 403


class AssistantUpstreamError(LoomServerError):
    """LibreChat could not give this answer: it was unreachable, refused the
    request, ended the answer with an error, or ran past the answer's limit. The
    message names none of LibreChat's text. Ask again: each answer spends model
    budget, so the SDKs do not resend one on their own."""

    code = "ASSISTANT_UPSTREAM_FAILED"
    status_code = 503

    def __init__(self, message: str = "", *, retry_after_s: int = 5) -> None:
        super().__init__(message, retry_after_s=retry_after_s)


# How a caller acts on an error without knowing its code: `category` says whose move it
# is, `retryable` whether resending the same request unchanged can succeed later (after
# `retry_after_s` when the error names one). The status decides unless the code is listed;
# an error that knows better (a refusal whose check could not run) sets its own.
ErrorCategory = Literal["input", "auth", "capacity", "dependency", "configuration", "internal"]

CLASS_BY_CODE: dict[str, tuple[ErrorCategory, bool]] = {
    # Busy or not ready, not down: the same request succeeds once that passes. On an op
    # that is not idempotent a handler's answer may follow a write (OVERLOADED too: each
    # statement waits for a pool slot), so there it is not retryable (`handler_failure_detail`).
    "OVERLOADED": ("capacity", True),
    "GRANT_BUSY": ("capacity", True),
    "IDEMPOTENT_IN_PROGRESS": ("capacity", True),
    "MODEL_CALL_DEFERRED": ("capacity", True),
    "MODEL_CHANGE_IN_PROGRESS": ("capacity", True),
    "CONFIG_DRIFT": ("capacity", True),
    "IDEMPOTENT_TAKEN_OVER": ("capacity", True),
    "ADMISSION_CONTENDED": ("capacity", True),
    "PROJECTION_BEHIND": ("capacity", True),
    # Committed already, and its message says not to make the write again.
    "PUBLICATION_PENDING": ("dependency", False),
    # Only an operator's fix ends these; resending changes nothing.
    "SCHEMA_PENDING": ("configuration", False),
    "OVERRIDE_NOT_STORED": ("configuration", False),
    "CODED_WIDTH_MISMATCH": ("configuration", False),
    "ANALYSIS_UNAVAILABLE": ("configuration", False),
    "ASSISTANT_UNAVAILABLE": ("configuration", False),
    "ASSISTANT_UPSTREAM_FAILED": ("dependency", True),
    "GATEWAY_REQUIRED": ("configuration", False),
    "INTEGRATION_NOT_CONFIGURED": ("configuration", False),
    "MODEL_CALLS_DISABLED": ("configuration", False),
    "NO_SIGNING_KEY": ("configuration", False),
    # Listed, not left to the status: the MCP screen's refusals carry none.
    "RATE_LIMITED": ("capacity", True),
    "AUTH_INVALID": ("auth", False),
    "AUTH_INSUFFICIENT": ("auth", False),
    "ASSISTANT_SELF_CALL": ("auth", False),
    "POLICY_FORBIDDEN": ("auth", False),
    "NAMESPACE_NOT_ACCESSIBLE": ("auth", False),
    "ORG_MISMATCH": ("auth", False),
}


# The refusals that can say their check could not run (a grant or a policy trace that could
# not be read); the errors reference lists them.
UnverifiedCode = Literal["AUTH_INSUFFICIENT", "POLICY_FORBIDDEN", "NAMESPACE_NOT_ACCESSIBLE"]
UNVERIFIED_CODES = frozenset(get_args(UnverifiedCode))


def unverified_class(code: UnverifiedCode, unverified: bool) -> dict[str, Any]:
    """The class of a `code` refusal whose check could not run: the caller may be allowed,
    so the same request can succeed once the store answers. Empty when the check ran.
    `code` is checked by its type only: mypy keeps each call site's code in
    `UNVERIFIED_CODES`, which the errors reference lists."""
    return {"category": "dependency", "retryable": True} if unverified else {}


def error_class(code: str, status: int) -> tuple[ErrorCategory, bool]:
    """`(category, retryable)` for an error `code` answered with HTTP `status`
    (0 for an MCP refusal that has none)."""
    if code in CLASS_BY_CODE:
        return CLASS_BY_CODE[code]
    if status in (401, 403):
        return "auth", False
    if status in (408, 429):
        return "capacity", True
    if status in (502, 503, 504):
        return "dependency", True
    if status >= 500:
        return "internal", False
    return "input", False


def classified(detail: dict[str, Any], status: int) -> dict[str, Any]:
    """`detail` with the `category` and `retryable` every coded error body carries,
    unless the error set them itself."""
    category, retryable = error_class(str(detail.get("code") or ""), status)
    return {
        **detail,
        "category": detail.get("category", category),
        "retryable": detail.get("retryable", retryable),
    }


def handler_failure_detail(detail: Any, status: int, idempotent: bool) -> Any:
    """A handler's 5xx on an op that is not idempotent may follow a write the call
    made, so resending it unchanged is not safe: `retryable` false. A 4xx, an idempotent
    op and a detail with no code (which carries no class at all) are left as they are."""
    if idempotent or status < 500 or not isinstance(detail, dict) or not detail.get("code"):
        return detail
    return {**detail, "retryable": False}


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
    coded HTTP error its `category` and `retryable` fields and a
    `Link: <catalogue#code>; rel="help"` header, and keep a request body's 422 JSON-safe."""
    from fastapi import Request
    from fastapi.encoders import jsonable_encoder
    from fastapi.exception_handlers import http_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(LoomServerError)
    async def _handle_loom_error(_: Request, exc: LoomServerError) -> JSONResponse:
        detail = classified(exc.to_detail(), exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=_with_help_link(exc.headers(), detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        headers = _with_help_link(getattr(exc, "headers", None), exc.detail)
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("code"):
            detail = classified(detail, exc.status_code)
        # Never write onto exc: a module-level HTTPException is raised on every request.
        answer = StarletteHTTPException(exc.status_code, detail=detail, headers=headers)
        return await http_exception_handler(request, answer)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422, except that a non-finite input (`1e400`) is echoed as a
        # string: JSON has no inf, and the response would fail as a 500.
        errors = loads_keeping_non_finite(json.dumps(jsonable_encoder(exc.errors())))
        return JSONResponse(status_code=422, content={"detail": errors})
