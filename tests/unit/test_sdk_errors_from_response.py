"""`loom.errors.from_response` maps a registered `detail.code` first and falls
back to the HTTP status for any other code or no code (a handler-specific
refusal, an unknown route, a proxy error page), so a caller can catch
`NotFound` / `PreconditionFailed` the way the TypeScript SDK's
`errorFromResponse` lets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
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
from loom.transport import Transport, unreachable_error

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
    "ASSISTANT_SELF_CALL": AuthInsufficient,
    "EMBED_TIMEOUT": BackendUnavailable,  # a 503 with no SDK class: mapped by status
    "GRANT_BUSY": BackendUnavailable,
    "NO_SIGNING_KEY": BackendUnavailable,  # a 503 with no SDK class: mapped by status
    "GATEWAY_REQUIRED": BackendUnavailable,  # a 503 with no SDK class, never retried
    "OVERRIDE_NOT_STORED": LoomError,  # a 409 refusal by ClickHouse: no precondition, not retried
    "ENTRY_LEASE_NOT_HELD": PreconditionFailed,  # the uploading pass no longer holds its entry
    "SOURCE_REPLACED": PreconditionFailed,  # a 409 with no SDK class: mapped by status
    "COLLECTION_NAME_TAKEN": PreconditionFailed,  # a 409 with no SDK class: mapped by status
    "WRONG_CELL": PreconditionFailed,  # a 409 with no SDK class: mapped by status
    "MODEL_CALL_UNATTRIBUTED": BackendUnavailable,  # a 500 with no SDK class: mapped by status
    "MODEL_CALLS_DISABLED": AuthInsufficient,  # a 403 with no SDK class: mapped by status
    "MODEL_CALL_REFUSED": AuthInsufficient,  # a 403 with no SDK class, never retried
    "MODEL_CALL_DEFERRED": BackendUnavailable,  # a 503 with no SDK class: mapped by status
    # The Postgres memory authority's 503s, retryable, with no SDK class of their own.
    "POSTGRES_UNAVAILABLE": BackendUnavailable,
    "ADMISSION_CONTENDED": BackendUnavailable,
    "PROJECTION_BEHIND": BackendUnavailable,
    "PUBLICATION_PENDING": BackendUnavailable,
    # A retry took over the request's key: a 503 with no SDK class, mapped by status.
    "IDEMPOTENT_TAKEN_OVER": BackendUnavailable,
    # Postgres refused a statement: a 500 with no SDK class, mapped by status.
    "POSTGRES_REFUSED": BackendUnavailable,
    "ANALYSIS_UNAVAILABLE": BackendUnavailable,  # a 503 with no SDK class, never retried
    "ASSISTANT_UNAVAILABLE": BackendUnavailable,  # a 503 with no SDK class, never retried
    "ASSISTANT_UPSTREAM_FAILED": BackendUnavailable,  # a 503 with no SDK class
}


def test_every_server_error_maps_to_its_documented_sdk_class() -> None:
    import server.auth.keyring  # defines NO_SIGNING_KEY
    import server.code.app.ingest  # defines ENTRY_LEASE_NOT_HELD
    import server.core.runtime_overrides  # defines OVERRIDE_NOT_STORED
    import server.identity.app.policy_gate  # defines POLICY_FORBIDDEN
    import server.memory.app.pg_admission  # noqa: F401 — defines ADMISSION_CONTENDED

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


def test_category_and_retryable_come_from_the_detail() -> None:
    body = {"detail": {"code": "LOCKED", "message": "m", "category": "input", "retryable": True}}
    e = from_response(body, 423)
    assert (e.category, e.retryable) == ("input", True)


def test_category_and_retryable_without_a_named_class() -> None:
    proxy_page = from_response({"detail": "Bad Gateway"}, 502)
    assert (proxy_page.category, proxy_page.retryable) == ("dependency", True)
    refused = from_response({"detail": "Forbidden"}, 403)
    assert (refused.category, refused.retryable) == ("auth", False)
    cut_off = LoomError("response cut off")
    cut_off.code = "NETWORK"
    assert (cut_off.category, cut_off.retryable) == ("dependency", False)
    misuse = LoomError("tool arguments are not valid JSON")
    assert (misuse.category, misuse.retryable) == ("input", False)


def test_a_configuration_code_without_the_fields_is_not_retryable() -> None:
    e = from_response({"detail": {"code": "GATEWAY_REQUIRED", "message": "m"}}, 503)
    assert (e.category, e.retryable) == ("configuration", False)


def test_an_error_raised_before_sending_is_classed_by_its_family() -> None:
    down = BackendUnavailable("cannot reach the identity provider")
    assert (down.category, down.retryable) == ("dependency", True)
    signed_out = AuthInvalid("not signed in; run loom login")
    assert (signed_out.category, signed_out.retryable) == ("auth", False)


def test_a_connection_the_server_never_saw_is_retryable() -> None:
    refused = unreachable_error("http://h", httpx.ConnectError("refused"))
    assert (refused.category, refused.retryable) == ("dependency", True)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.LocalProtocolError("bad header"), "input"),
        (httpx.ProxyError("407"), "dependency"),
    ],
)
def test_a_request_that_could_not_be_sent_is_not_retryable_on_any_op(
    error: httpx.HTTPError, category: str
) -> None:
    calls: list[str] = []

    def answer(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        raise error

    err = _sent("memory.set", answer, attempts=2)  # resent if it could have run
    assert (err.code, err.category, err.retryable) == ("NETWORK", category, False)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "localhost:7777",
        "ftp://loom.test",
        "ftp://u:p@loom.test",
        "ftp://u@loom.test:x",
        "http://[::1",
        "http://",
        "https:///v1",
    ],
)
def test_a_base_url_that_cannot_be_sent_is_refused_when_the_transport_is_built(
    base_url: str,
) -> None:
    with pytest.raises(LoomError) as info:
        Transport(base_url)
    assert (info.value.category, info.value.retryable) == ("input", False)
    assert "u:p" not in str(info.value)


def test_a_base_url_with_credentials_is_accepted() -> None:
    # Unlike the TypeScript SDK, whose fetch cannot send to one: httpx sends them.
    assert Transport("http://u:p@loom.test").base_url == "http://u:p@loom.test"


def test_a_fieldless_model_call_with_no_customer_is_internal() -> None:
    e = from_response({"detail": {"code": "MODEL_CALL_UNATTRIBUTED", "message": "m"}}, 500)
    assert (e.category, e.retryable) == ("internal", False)


def _sent(
    op: str,
    answer: Callable[[httpx.Request], httpx.Response],
    *,
    attempts: int = 1,
    **retry: object,
) -> LoomError:
    """The error `op` raises through the transport, its requests answered by `answer`."""

    async def call() -> LoomError:
        policy = {"max_attempts": attempts, "backoff_s": 0, **retry}
        t = Transport("http://loom.test", retry=policy)
        t._client = httpx.AsyncClient(
            transport=httpx.MockTransport(answer), base_url="http://loom.test"
        )
        try:
            await t.post(f"/v1/{op}", {})
        except LoomError as e:
            return e
        finally:
            await t.close()
        raise AssertionError("no error")

    return asyncio.run(call())


def _fieldless_503(op: str, code: str, **named: object) -> LoomError:
    """`op` answered by a 503 naming `code` and no class (or the class in `named`)."""
    detail = {"code": code, "message": "m", **named}
    return _sent(op, lambda _req: httpx.Response(503, json={"detail": detail}))


def test_a_fieldless_5xx_on_a_write_that_is_not_idempotent_is_not_retryable() -> None:
    write = _fieldless_503("memory.set_from_messages", "BACKEND_UNAVAILABLE")
    assert (write.category, write.retryable) == ("dependency", False)
    read = _fieldless_503("memory.get", "BACKEND_UNAVAILABLE")
    assert (read.category, read.retryable) == ("dependency", True)


def test_a_fieldless_overloaded_is_retryable_only_on_an_idempotent_op() -> None:
    """A handler's OVERLOADED may follow a statement that wrote, as the server's rule says."""
    write = _fieldless_503("memory.set_from_messages", "OVERLOADED")
    assert (write.category, write.retryable) == ("dependency", False)
    read = _fieldless_503("memory.get", "OVERLOADED")
    assert (read.category, read.retryable) == ("dependency", True)


def test_a_5xx_the_handler_never_ran_or_the_server_calls_retryable_stays_retryable() -> None:
    """On an op that is not idempotent: a code that proves the handler never ran, and a
    body whose class says retryable, keep `retryable`."""
    unloaded = _fieldless_503("memory.set_from_messages", "AUTH_UNAVAILABLE")
    assert (unloaded.category, unloaded.retryable) == ("dependency", True)
    named = _fieldless_503(
        "memory.set_from_messages", "MODEL_CHANGE_IN_PROGRESS", category="capacity", retryable=True
    )
    assert (named.category, named.retryable) == ("capacity", True)


@pytest.mark.parametrize(
    ("op", "retryable"),
    [("memory.set", True), ("memory.search", True), ("memory.set_from_messages", False)],
)
def test_a_timed_out_call_is_retryable_only_on_an_idempotent_op(op: str, retryable: bool) -> None:
    def answer(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    err = _sent(op, answer)
    assert (err.category, err.retryable) == ("dependency", retryable)


@pytest.mark.parametrize(
    ("error", "op", "retryable"),
    [
        (httpx.ReadError("eof"), "memory.search", True),
        (httpx.ReadError("eof"), "memory.set_from_messages", False),
        (httpx.WriteError("broken pipe"), "memory.search", True),
        (httpx.WriteError("broken pipe"), "memory.set_from_messages", False),
    ],
)
def test_a_call_that_may_have_run_is_retryable_only_on_an_idempotent_op(
    error: httpx.HTTPError, op: str, retryable: bool
) -> None:
    def answer(_req: httpx.Request) -> httpx.Response:
        raise error

    err = _sent(op, answer)
    assert (err.code, err.category, err.retryable) == ("NETWORK", "dependency", retryable)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("eof"),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("peer closed"),
        httpx.DecodingError("bad gzip"),
    ],
)
@pytest.mark.parametrize(
    ("op", "sent"),
    [("memory.set", 2), ("memory.ask", 1), ("memory.search", 1), ("memory.set_from_messages", 1)],
)
def test_a_call_that_may_have_run_is_resent_only_where_the_server_dedups_it(
    error: httpx.HTTPError, op: str, sent: int
) -> None:
    calls: list[str] = []

    def answer(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        raise error

    _sent(op, answer, attempts=2)
    assert len(calls) == sent


@pytest.mark.parametrize(
    ("policy", "op", "sent"),
    [
        ({"retry_timeouts": "never"}, "memory.set", 1),
        ({"retry_timeouts": "always"}, "memory.set_from_messages", 2),
        ({"retry_connection_errors": False}, "memory.set", 2),
    ],
)
def test_the_timeout_setting_governs_a_call_that_may_have_run(
    policy: dict[str, object], op: str, sent: int
) -> None:
    calls: list[str] = []

    def answer(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        raise httpx.ReadError("eof")

    _sent(op, answer, attempts=2, **policy)
    assert len(calls) == sent


def test_an_answer_the_server_classes_configuration_is_not_retried() -> None:
    calls: list[str] = []

    def answer(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        detail = {"code": "SCHEMA_PENDING", "message": "m", "category": "configuration"}
        return httpx.Response(503, json={"detail": {**detail, "retryable": False}})

    err = _sent("memory.get", answer, attempts=3)
    assert (err.retryable, len(calls)) == (False, 1)
