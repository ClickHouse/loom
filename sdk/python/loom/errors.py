"""SDK exception hierarchy mirroring server error codes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast, get_args

#: Whose move an error is (`LoomError.category`).
ErrorCategory = Literal["input", "auth", "capacity", "dependency", "configuration", "internal"]


class LoomError(Exception):
    """Base class for all SDK errors.

    `code` is the server's `detail.code`; `request_id` is the `X-Request-Id`
    the server echoed (or the one this SDK sent), so a failure can be found
    in the server's ledger. `status` is the HTTP status, 0 when the request
    never got a response. `detail` is the server's `detail` object — `code`,
    `message` and whatever the error adds (`errors`, `table`,
    `retry_after_s`, …); `{}` when the response carried none.
    """

    code: str = "INTERNAL_ERROR"
    request_id: str = ""
    status: int = 0
    detail: dict[str, Any]
    #: The SDK's own `(category, retryable)` for this error, used when the server's
    #: `detail` names none.
    _sdk_class: tuple[ErrorCategory, bool] | None = None

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.detail = {}

    @property
    def category(self) -> ErrorCategory:
        """Whose move it is: the server's `detail.category` (`input`, `auth`,
        `capacity`, `dependency`, `configuration` or `internal`), else the SDK's own
        classing (the Errors section of the Python SDK reference)."""
        named = self.detail.get("category")
        if named in get_args(ErrorCategory):
            return cast(ErrorCategory, named)
        return _fallback_class(self)[0]

    @property
    def retryable(self) -> bool:
        """The server's `detail.retryable`: resending the same request unchanged
        can succeed later. Else the SDK's own classing, as for `category`."""
        named = self.detail.get("retryable")
        return named if isinstance(named, bool) else _fallback_class(self)[1]


class ValidationError(LoomError):
    code = "VALIDATION_FAILED"


class AuthInvalid(LoomError):
    code = "AUTH_INVALID"


class AuthInsufficient(LoomError):
    code = "AUTH_INSUFFICIENT"


class NotFound(LoomError):
    code = "RESOURCE_NOT_FOUND"


class BackendUnavailable(LoomError):
    code = "BACKEND_UNAVAILABLE"


#: 5xx codes never resent, with their category: a deployment's configuration (the
#: operator's fix is named in the error and in /v1/health), a model call that names
#: no customer (a server path to fix, not a passing fault), and a write already
#: committed whose rows are still on their way.
_NEVER_RETRIED: Mapping[str, ErrorCategory] = {
    "ANALYSIS_UNAVAILABLE": "configuration",
    "ASSISTANT_UNAVAILABLE": "configuration",
    "CODED_WIDTH_MISMATCH": "configuration",
    "GATEWAY_REQUIRED": "configuration",
    "MODEL_CALL_UNATTRIBUTED": "internal",
    "PUBLICATION_PENDING": "dependency",
}

#: The wait assumed when the server names none (or one past the cap).
DEFAULT_RETRY_AFTER_S = 1.0


class RateLimited(BackendUnavailable):
    """Rate-limited by the server.

    Subclasses BackendUnavailable so callers catching the broader class still
    match. Carries `retry_after_s` (from the server's Retry-After header) so
    retry layers can honor the server's hint.
    """

    code = "RATE_LIMITED"
    retry_after_s: float = DEFAULT_RETRY_AFTER_S

    def __init__(self, message: str = "", retry_after_s: float = DEFAULT_RETRY_AFTER_S) -> None:
        super().__init__(message or f"rate-limited; retry-after={retry_after_s:g}s")
        self.retry_after_s = retry_after_s


class Overloaded(BackendUnavailable):
    """503 OVERLOADED — every pooled ClickHouse connection of the serving
    process was busy for longer than its slot wait. Retry
    after `retry_after_s`; a client that spreads its calls or lowers its
    concurrency stops seeing it."""

    code = "OVERLOADED"
    retry_after_s: float = DEFAULT_RETRY_AFTER_S

    def __init__(self, message: str = "", retry_after_s: float = DEFAULT_RETRY_AFTER_S) -> None:
        super().__init__(message or f"server overloaded; retry-after={retry_after_s:g}s")
        self.retry_after_s = retry_after_s


class IdempotentInProgress(BackendUnavailable):
    """503 IDEMPOTENT_IN_PROGRESS — the first request with this
    Idempotency-Key still runs. Retried after `retry_after_s` (the transport
    does, for an idempotent op); the retry gets the first request's answer."""

    code = "IDEMPOTENT_IN_PROGRESS"
    retry_after_s: float = DEFAULT_RETRY_AFTER_S

    def __init__(self, message: str = "", retry_after_s: float = DEFAULT_RETRY_AFTER_S) -> None:
        super().__init__(message or f"request still running; retry-after={retry_after_s:g}s")
        self.retry_after_s = retry_after_s


class BackendTimeout(BackendUnavailable):
    """504 BACKEND_TIMEOUT — a ClickHouse statement behind the call ran past
    its class deadline and was not retried."""

    code = "BACKEND_TIMEOUT"


class SchemaPending(BackendUnavailable):
    """503 SCHEMA_PENDING — the call needed a table that a migration the
    server's build carries has not yet been applied to its ClickHouse.
    `detail` names `table` and `migration`; retry once the
    service has migrated."""

    code = "SCHEMA_PENDING"


class CodedWidthMismatch(BackendUnavailable):
    """503 CODED_WIDTH_MISMATCH — the server's embedder writes vectors of a width
    its coded vector tables do not take; `/v1/health` says how to fix it."""

    code = "CODED_WIDTH_MISMATCH"


#: `code` of a `BackendUnavailable` for a request that got no complete response:
#: the server could not be reached, the request could not be sent, or the call may
#: have run (cut off, broken off or timed out); `.retryable` says which.
NETWORK_ERROR_CODE = "NETWORK"
#: `code` of a `LoomError` for a 2xx whose body is not a JSON object — a proxy
#: or gateway page, most often. Not retried: the server may have acted.
INVALID_RESPONSE_CODE = "INVALID_RESPONSE"


def _set_sdk_class(err: LoomError, category: ErrorCategory, retryable: bool) -> None:
    """Give `err` the SDK's own class, used when the server's `detail` names none."""
    err._sdk_class = (category, retryable)


def _busy(message: str, status: int) -> BackendUnavailable:
    """A sign-in answer that can pass later (a 429 or a 5xx), classed retryable."""
    err = BackendUnavailable(message)
    _set_sdk_class(err, "capacity" if status == 429 else "dependency", True)
    return err


def _fallback_class(e: LoomError) -> tuple[ErrorCategory, bool]:
    """The class of an error whose body names none: one the SDK raised itself (no
    response, an unreadable answer, a call it could not send), a code the SDK never
    retries, else the server's rule by HTTP status (a proxy's page, or a server
    without the fields)."""
    if e._sdk_class is not None:
        return e._sdk_class
    if e.code == NETWORK_ERROR_CODE:
        return "dependency", False
    if e.code == INVALID_RESPONSE_CODE:
        return "internal", False
    if e.code in _NEVER_RETRIED:
        return _NEVER_RETRIED[e.code], False
    status = e.status
    if status == 0:
        # Raised before the call was sent: a service the SDK needed first (an identity
        # provider, a secret store) is down, the caller is not signed in, or the call
        # itself is wrong.
        if isinstance(e, BackendUnavailable):
            return "dependency", True
        if isinstance(e, (AuthInvalid, AuthInsufficient)):
            return "auth", False
        return "input", False
    if status in (401, 403):
        return "auth", False
    if status in (408, 429):
        return "capacity", True
    if status in (502, 503, 504):
        return "dependency", True
    if status >= 500:
        return "internal", False
    return "input", False


class IdempotentConflict(LoomError):
    """An Idempotency-Key was reused with a different request body."""

    code = "IDEMPOTENT_CONFLICT"


class PreconditionFailed(LoomError):
    """An if_match_sha256 precondition didn't match the live row's hash, or an
    erasure landed while a namespace.restore ran (retry the restore; no hash).

    A mismatch carries the server's current hash in `.current_sha256` so a caller
    can re-read and retry without an extra get round-trip.
    """

    code = "PRECONDITION_FAILED"
    current_sha256: str = ""

    def __init__(self, message: str = "", current_sha256: str = "") -> None:
        super().__init__(message)
        self.current_sha256 = current_sha256


ERROR_CODES: dict[str, type[LoomError]] = {
    "VALIDATION_FAILED": ValidationError,
    "AUTH_INVALID": AuthInvalid,
    "AUTH_INSUFFICIENT": AuthInsufficient,
    "RESOURCE_NOT_FOUND": NotFound,
    "RATE_LIMITED": RateLimited,
    "BACKEND_UNAVAILABLE": BackendUnavailable,
    "OVERLOADED": Overloaded,
    "BACKEND_TIMEOUT": BackendTimeout,
    "SCHEMA_PENDING": SchemaPending,
    "CODED_WIDTH_MISMATCH": CodedWidthMismatch,
    "IDEMPOTENT_CONFLICT": IdempotentConflict,
    "IDEMPOTENT_IN_PROGRESS": IdempotentInProgress,
    "PRECONDITION_FAILED": PreconditionFailed,
    # ClickHouse refused to store an override: no precondition, and not retried.
    "OVERRIDE_NOT_STORED": LoomError,
}


# HTTP status → error class when the body carries no `detail.code` (a route the
# server does not have answers FastAPI's plain `{"detail": "Not Found"}`; a
# proxy may answer text). Mirrors the TypeScript SDK's `errorFromResponse`.
_STATUS_CLASSES: dict[int, type[LoomError]] = {
    400: ValidationError,
    401: AuthInvalid,
    403: AuthInsufficient,
    404: NotFound,
    409: PreconditionFailed,
    412: PreconditionFailed,
    422: ValidationError,
}


def from_response(
    payload: object,
    status: int | None = None,
    *,
    request_id: str = "",
    retry_after_s: float | None = None,
) -> LoomError:
    """The exception for a server error body: a known `detail.code` picks the
    class, else the HTTP `status` does (the table above; any other 5xx is
    `BackendUnavailable`), so a handler-specific code such as
    `PINNED_CONFLICT` or a proxy's error page still raises a class a caller
    can catch. A 429 is always `RateLimited`, a 5xx always a
    `BackendUnavailable`. `.code` keeps the server's code; `.detail` its
    detail object; `retry_after_s` lands on the classes that carry it."""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    code = ""
    if isinstance(detail, dict):
        code = str(detail.get("code") or "")
        message = str(detail.get("message", ""))
    elif isinstance(detail, str):
        message = detail
    else:
        message = str(payload)[:500]
    status = int(status or 0)
    cls = ERROR_CODES.get(code) or _STATUS_CLASSES.get(status, LoomError)
    if status == 429:
        cls = RateLimited
    elif status >= 500 and not issubclass(cls, BackendUnavailable):
        cls = BackendUnavailable
    code = code or cls.code
    err = cls(f"[{code}] {message}" if message else code)
    err.code = code
    err.status = status
    err.request_id = request_id
    if isinstance(detail, dict):
        err.detail = dict(detail)
    elif message:
        err.detail = {"message": message}
    if retry_after_s is not None and hasattr(err, "retry_after_s"):
        err.retry_after_s = float(retry_after_s)
    if "current_sha256" in err.detail and hasattr(err, "current_sha256"):
        err.current_sha256 = str(err.detail["current_sha256"])
    return err
