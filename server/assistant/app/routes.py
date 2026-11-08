"""``POST /v1/assistant/chat/completions``: the assistant, as an OpenAI-compatible chat.

The caller's own Loom token (a dashboard session's, an SDK's, an agent's) asks one
question about the namespace `X-Loom-Namespace` names (the token's default namespace
when absent), with the earlier turns in `messages`; Loom stores no conversation. The
answer streams as chat-completion chunks (`stream: true`) or comes back whole, and
leaves one ledger row (`operation = assistant.ask`) carrying its model spend and the
answer's id as `run_id`, the id its tool calls are ledgered under. The route is admitted
as an op is: `assistant.ask`'s rate-limit buckets before anything runs, and one of the
org's in-flight slots while the answer runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from server.assistant.app import bridge, service
from server.auth.authz import check_acted_for
from server.auth.bearer import missing_bearer, verify_bearer_header
from server.auth.context import set_current_auth
from server.auth.gateway import effective_auth
from server.auth.jwt import AuthInfo
from server.core import fail_open, tenant_slots
from server.core.errors import (
    AssistantUnavailableError,
    AuthInsufficientError,
    LoomServerError,
    RateLimitedError,
    ValidationError,
)
from server.identity.app.caller_home import default_namespace
from server.identity.app.restricted_access import note_restricted_access
from server.observability import cost_accumulator, sli
from server.observability.budgets import effective_budget_ms
from server.observability.ledger import emit_agent_event
from server.settings import settings
from server.settings.catalog import OP_BUDGETS_P99_MS

log = logging.getLogger(__name__)

router = APIRouter()

# What a client that went away is recorded as: its own doing, not the server's.
_CLIENT_CLOSED = "HTTP_499"


def _refusal(e: BaseException) -> LoomServerError | HTTPException:
    """A refusal raised by a check the route shares with the op path, as Loom answers it
    there: an access refusal is AUTH_INSUFFICIENT, an HTTP refusal stays as it is."""
    if isinstance(e, LoomServerError | HTTPException):
        return e
    log.debug("assistant refused: %s", e)
    return AuthInsufficientError("the caller may not ask the assistant here")


async def _caller(request: Request) -> AuthInfo:
    auth = verify_bearer_header(request.headers.get("Authorization"), acts_for_end_users=True)
    if auth is None:
        if not settings.auth_actually_required():
            # Each answer reads under a token delegated from the caller's.
            raise AssistantUnavailableError("the assistant answers as its caller: it needs auth on")
        raise missing_bearer()
    try:
        auth = effective_auth(auth, request.headers.get("X-Loom-On-Behalf-Of") or "")
    except PermissionError as e:
        raise _refusal(e) from e
    set_current_auth(auth)
    return auth


def _admit(auth: AuthInfo, org: str, namespace: str) -> None:
    """`assistant.ask`'s rate-limit buckets, as the transports draw them for the op; a
    limiter that fails admits the request, as it admits an op."""
    from server.core.ratelimit import get_limiter

    try:
        rl = get_limiter().check(namespace, bridge.ASSISTANT_OP, org=org, actor=auth.subject)
    except Exception as e:  # noqa: BLE001 — the limiter fails open: the request is admitted
        fail_open.note("rate_limit", bridge.ASSISTANT_OP, e)
        return
    if not rl.allowed:
        wait = max(1, int(rl.retry_after_s + 0.99))
        raise RateLimitedError("rate limit exceeded; retry later", retry_after_s=wait)


class _Row:
    """The answer's one ledger row, written when it ends however it ends."""

    def __init__(self, auth: AuthInfo, org: str, namespace: str) -> None:
        self.auth, self.org, self.namespace = auth, org, namespace
        self.request_id = uuid.uuid4().hex
        self.started = time.perf_counter()
        cost_accumulator.begin_op()  # the answer's spend lands on this row

    def write(self, error: BaseException | None) -> None:
        if error is None:
            code = ""
        elif isinstance(error, LoomServerError):
            code = error.code
        elif isinstance(error, HTTPException):
            code = f"HTTP_{error.status_code}"
        elif isinstance(error, asyncio.CancelledError | GeneratorExit):
            code = _CLIENT_CLOSED
        else:
            code = "INTERNAL_ERROR"
        try:
            emit_agent_event(
                op_name=bridge.ASSISTANT_OP,
                identity={
                    "org": self.org,
                    "namespace": self.namespace,
                    "agent": "-",
                    "user_id": "-",
                    "session_id": "-",
                },
                request_id=self.request_id,
                latency_ms=int((time.perf_counter() - self.started) * 1000),
                success=error is None,
                error_code=code,
                error=None if code == _CLIENT_CLOSED else error,
                impersonated_by=self.auth.impersonated_by or "",
                # The caller's own client asked: its tool calls are `loom-assistant`'s.
                subject=self.auth.subject,
                transport="http",
                slo_class=sli.slo_class(bridge.ASSISTANT_OP, writes=False, admin=False),
                budget_ms=effective_budget_ms(
                    bridge.ASSISTANT_OP, OP_BUDGETS_P99_MS[bridge.ASSISTANT_OP]
                ),
            )
        except Exception as e:  # noqa: BLE001 — telemetry never fails an answer
            log.debug("assistant answer: ledger row not written: %s", e)


@router.post("/v1/assistant/chat/completions")
async def chat_completions(request: Request) -> Any:
    """One answer from the Loom assistant. Body: OpenAI's chat request (`messages`,
    `stream`); any `model`, `conversation_id`, system message, tool or file is ignored.
    The caller needs `viewer` on the namespace."""
    auth = await _caller(request)
    try:
        payload = await request.json()
    except ValueError as e:
        raise ValidationError("the body must be JSON") from e
    if not isinstance(payload, dict):
        raise ValidationError("the body must be a JSON object")
    org = auth.org
    namespace = request.headers.get("X-Loom-Namespace") or await default_namespace(auth, org)
    if not namespace:
        raise ValidationError("name a namespace (X-Loom-Namespace)")
    row = _Row(auth, org, namespace)
    try:
        messages = bridge.admitted_messages(payload.get("messages"))
        _admit(auth, org, namespace)
        await check_acted_for(auth, org)
        ticket = await bridge.open_answer(auth, org, namespace)
    except (LoomServerError, HTTPException, PermissionError) as e:
        refused = _refusal(e)
        row.write(refused)  # a refused answer leaves its row, as a refused op does
        raise refused from e
    try:
        await note_restricted_access(
            auth, op=bridge.ASSISTANT_OP, org=org, namespace=namespace, transport="http"
        )
    except BaseException as e:
        ticket.release()
        row.write(e)
        raise
    answer = bridge.Answer()
    chunks = bridge.stream(ticket, messages, answer)
    if payload.get("stream"):
        return StreamingResponse(
            _streamed(chunks, ticket, row),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Request-Id": row.request_id},
        )
    return await _whole(chunks, ticket, answer, row)


async def _streamed(
    chunks: AsyncIterator[bytes], ticket: bridge.Ticket, row: _Row
) -> AsyncIterator[bytes]:
    error: BaseException | None = None
    try:
        with tenant_slots.hold(ticket.org):
            async for chunk in chunks:
                yield chunk
    except LoomServerError as e:
        # The status is sent: the refusal goes out as an error event.
        error = e
        yield bridge.error_event(e)
    except BaseException as e:  # a disconnect cancels the stream
        error = e
        raise
    finally:
        ticket.release()  # a stream cancelled before it began never gave it back
        row.write(error)


async def _whole(
    chunks: AsyncIterator[bytes], ticket: bridge.Ticket, answer: bridge.Answer, row: _Row
) -> JSONResponse:
    error: BaseException | None = None
    try:
        with tenant_slots.hold(ticket.org):
            async for _ in chunks:
                pass
    except BaseException as e:
        error = e
        raise
    finally:
        ticket.release()
        row.write(error)
    return JSONResponse(
        {
            "id": answer.run_id,
            "object": "chat.completion",
            "model": service.current_definition().model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(answer.text)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": answer.prompt_tokens,
                "completion_tokens": answer.completion_tokens,
                "total_tokens": answer.prompt_tokens + answer.completion_tokens,
            },
        },
        headers={"X-Request-Id": row.request_id},
    )


def attach(app: Any) -> None:
    app.include_router(router)
