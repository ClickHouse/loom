"""`loom.errors.from_response` maps a registered `detail.code` first and falls
back to the HTTP status for any other code or no code (a handler-specific
refusal, an unknown route, a proxy error page), so a caller can catch
`NotFound` / `PreconditionFailed` the way the TypeScript SDK's
`errorFromResponse` lets it.
"""

from __future__ import annotations

from loom.errors import (
    AuthInsufficient,
    AuthInvalid,
    BackendTimeout,
    BackendUnavailable,
    CodedWidthMismatch,
    IdempotentConflict,
    IdempotentInProgress,
    LoomError,
    NotFound,
    Overloaded,
    PreconditionFailed,
    RateLimited,
    SchemaPending,
    ValidationError,
    from_response,
)

from server.core.errors import LoomServerError


def test_code_in_detail_wins_over_status() -> None:
    e = from_response({"detail": {"code": "RESOURCE_NOT_FOUND", "message": "no row"}}, 200)
    assert isinstance(e, NotFound) and e.code == "RESOURCE_NOT_FOUND" and "no row" in str(e)


def test_unknown_route_404_is_not_found() -> None:
    e = from_response({"detail": "Not Found"}, 404)
    assert isinstance(e, NotFound)
    assert e.code == "RESOURCE_NOT_FOUND" and "Not Found" in str(e)


def test_status_fallbacks() -> None:
    assert isinstance(from_response({"detail": "x"}, 400), ValidationError)
    assert isinstance(from_response({"detail": "x"}, 422), ValidationError)
    assert isinstance(from_response({"detail": "x"}, 401), AuthInvalid)
    assert isinstance(from_response({"detail": "x"}, 403), AuthInsufficient)
    assert isinstance(from_response({"detail": "x"}, 409), PreconditionFailed)
    assert isinstance(from_response({"detail": "x"}, 412), PreconditionFailed)


def test_idempotent_conflict_on_409() -> None:
    e = from_response({"detail": {"code": "IDEMPOTENT_CONFLICT", "message": "reused"}}, 409)
    assert isinstance(e, IdempotentConflict)


def test_text_body_and_unknown_status_stay_generic() -> None:
    e = from_response("<html>bad gateway</html>", 418)
    assert type(e) is LoomError and e.code == "INTERNAL_ERROR" and "bad gateway" in str(e)
    e2 = from_response({"detail": {"code": "SOMETHING_NEW", "message": "m"}})
    assert type(e2) is LoomError and e2.code == "SOMETHING_NEW"


def test_legacy_single_argument_call_still_works() -> None:
    e = from_response({"detail": {"code": "VALIDATION_FAILED", "message": "bad"}})
    assert isinstance(e, ValidationError)


def test_unregistered_code_falls_back_to_status_class_and_keeps_code() -> None:
    cases = [
        (404, "NOT_FOUND", NotFound),
        (403, "FORBIDDEN", AuthInsufficient),
        (409, "PINNED_CONFLICT", PreconditionFailed),
        (409, "KEY_HOLDS_FACT", PreconditionFailed),
        (422, "BAD_NAMESPACE", ValidationError),
    ]
    for status, code, cls in cases:
        e = from_response({"detail": {"code": code, "message": "m"}}, status)
        assert type(e) is cls, (status, code, type(e))
        assert e.code == code and e.status == status


def test_unregistered_code_on_unmapped_status_stays_generic() -> None:
    e = from_response({"detail": {"code": "LOCKED", "message": "held"}}, 423)
    assert type(e) is LoomError and e.code == "LOCKED" and e.status == 423


def test_detail_carries_what_the_server_said() -> None:
    pending = {"code": "SCHEMA_PENDING", "message": "m", "table": "loom.x", "migration": "0999"}
    e = from_response({"detail": pending}, 503)
    assert isinstance(e, SchemaPending) and e.detail == pending
    invalid = {"code": "VALIDATION_FAILED", "message": "bad", "errors": [{"loc": ["top_k"]}]}
    assert from_response({"detail": invalid}, 422).detail["errors"] == [{"loc": ["top_k"]}]
    assert from_response({"detail": "Not Found"}, 404).detail == {"message": "Not Found"}
    assert from_response("", 500).detail == {}
    assert LoomError("raised by hand").detail == {}


def test_a_5xx_is_backend_unavailable_and_a_429_rate_limited_whatever_the_code() -> None:
    page = "<html>" + "x" * 10_000 + "</html>"
    e = from_response(page, 502)
    assert type(e) is BackendUnavailable and e.code == "BACKEND_UNAVAILABLE"
    assert len(str(e)) < 550  # a proxy page is quoted, not carried whole
    e = from_response({"detail": {"code": "VALIDATION_FAILED", "message": "m"}}, 500)
    assert type(e) is BackendUnavailable and e.code == "VALIDATION_FAILED"
    e = from_response({"detail": {"code": "SOMETHING_NEW", "message": "m"}}, 503)
    assert type(e) is BackendUnavailable and e.code == "SOMETHING_NEW"
    e = from_response({"detail": {"code": "RATE_LIMITED", "message": "m"}}, 429, retry_after_s=3)
    assert isinstance(e, RateLimited) and e.retry_after_s == 3.0
    assert isinstance(from_response("", 429), RateLimited)


# Every server error code and the SDK class a caller catches it as (docs/reference/python-sdk.md).
_SERVER_CODE_CLASSES: dict[str, type[LoomError]] = {
    "VALIDATION_FAILED": ValidationError,
    "AUTH_INSUFFICIENT": AuthInsufficient,
    "IDENTITY_INCOMPLETE": ValidationError,
    "RESOURCE_NOT_FOUND": NotFound,
    "PRECONDITION_FAILED": PreconditionFailed,
    "GRANT_WOULD_DOWNGRADE": PreconditionFailed,
    "READS_LLM_FREE": PreconditionFailed,
    "OVERLOADED": Overloaded,
    "RATE_LIMITED": RateLimited,
    "BACKEND_UNAVAILABLE": BackendUnavailable,
    "BACKEND_TIMEOUT": BackendTimeout,
    "SCHEMA_PENDING": SchemaPending,
    "CODED_WIDTH_MISMATCH": CodedWidthMismatch,
    "POLICY_FORBIDDEN": AuthInsufficient,
    "EMBED_TIMEOUT": BackendUnavailable,  # a 503 with no SDK class: mapped by status
    "GRANT_BUSY": BackendUnavailable,
    "NO_SIGNING_KEY": BackendUnavailable,  # a 503 with no SDK class: mapped by status
    "GATEWAY_REQUIRED": BackendUnavailable,  # a 503 with no SDK class, never retried
    "OVERRIDE_NOT_STORED": LoomError,  # a 409 refusal by ClickHouse: no precondition, not retried
    "ENTRY_LEASE_NOT_HELD": PreconditionFailed,  # the uploading pass no longer holds its entry
    "SOURCE_REPLACED": PreconditionFailed,  # a 409 with no SDK class: mapped by status
    "MODEL_CALL_UNATTRIBUTED": BackendUnavailable,  # a 500 with no SDK class: mapped by status
    "MODEL_CALLS_DISABLED": AuthInsufficient,  # a 403 with no SDK class: mapped by status
    "MODEL_CALL_REFUSED": AuthInsufficient,  # a 403 with no SDK class, never retried
    "MODEL_CALL_DEFERRED": BackendUnavailable,  # a 503 with no SDK class: mapped by status
}


def test_every_server_error_maps_to_its_documented_sdk_class() -> None:
    import server.auth.keyring  # defines NO_SIGNING_KEY
    import server.code.app.ingest  # defines ENTRY_LEASE_NOT_HELD
    import server.core.runtime_overrides  # defines OVERRIDE_NOT_STORED
    import server.identity.app.policy_gate  # noqa: F401 — defines POLICY_FORBIDDEN

    def subclasses(cls: type[LoomServerError]) -> list[type[LoomServerError]]:
        return [s for c in cls.__subclasses__() for s in (c, *subclasses(c))]

    seen = {c.code: c for c in subclasses(LoomServerError)}
    assert set(seen) == set(_SERVER_CODE_CLASSES), "a new server error code needs a row here"
    for code, cls in seen.items():
        e = from_response({"detail": {"code": code, "message": "m"}}, cls.status_code)
        assert type(e) is _SERVER_CODE_CLASSES[code], (code, cls.status_code, type(e))
        assert e.code == code and e.status == cls.status_code


def test_a_key_still_running_is_retryable_after_its_retry_after() -> None:
    """The transport's answers, which no server error class raises."""
    e = from_response(
        {"detail": {"code": "IDEMPOTENT_IN_PROGRESS", "message": "m", "in_progress": True}},
        503,
        retry_after_s=1,
    )
    assert type(e) is IdempotentInProgress and isinstance(e, BackendUnavailable)
    assert e.retry_after_s == 1.0
    e = from_response({"detail": {"code": "IDEMPOTENT_CONFLICT", "message": "m"}}, 409)
    assert type(e) is IdempotentConflict
