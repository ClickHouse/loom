"""``POST /v1/assistant/chat/completions``: the assistant, as an OpenAI-compatible chat.

The caller's own Loom token (a dashboard session's, an SDK's, an agent's) asks one
question about the namespace `X-Loom-Namespace` names (the token's default namespace
when absent), with the earlier turns in `messages`; Loom stores no conversation. The
answer streams as chat-completion chunks (`stream: true`) or comes back whole, and
leaves one ledger row (`operation = assistant.ask`) carrying its model spend and the
answer's id as `run_id`, the id its tool calls are ledgered under. The route is admitted
as an op is: once the caller is authorized, `assistant.ask`'s rate-limit buckets and one
of the org's in-flight slots, held until the response ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from server.assistant.app import bridge, service
from server.auth.authz import check_acted_for
from server.auth.bearer import missing_bearer, verify_bearer_header
from server.auth.context import set_current_auth
from server.auth.gateway import effective_auth
from server.auth.jwt import AuthInfo
from server.core import tenant_slots
from server.core.errors import (
    AssistantUnavailableError,
    AuthInsufficientError,
    LoomServerError,
    ValidationError,
)
from server.core.ratelimit import admit_route
from server.identity.app.caller_home import default_namespace
from server.identity.app.restricted_access import note_restricted_access
from server.observability import cost_accumulator, sli
from server.observability.budgets import effective_budget_ms
from server.observability.ledger import emit_agent_event
from server.observability.run_context import set_ledger_run
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


def _bearer(request: Request) -> AuthInfo:
    auth = verify_bearer_header(request.headers.get("Authorization"), acts_for_end_users=True)
    if auth is None:
        if not settings.auth_actually_required():
            # Each answer reads under a token delegated from the caller's.
            raise AssistantUnavailableError("the assistant answers as its caller: it needs auth on")
        raise missing_bearer()
    return auth


class _Row:
    """The answer's one ledger row, written once, when it ends however it ends."""

    def __init__(self, auth: AuthInfo, org: str, namespace: str) -> None:
        self.auth, self.org, self.namespace = auth, org, namespace
        self.request_id = uuid.uuid4().hex
        self.started = time.perf_counter()
        self.written = False
        cost_accumulator.begin_op()  # the answer's spend lands on this row

    def write(self, error: BaseException | None) -> None:
        if self.written:
            return
        self.written = True
        if error is None:
            code = ""
        elif isinstance(error, LoomServerError):
            code = error.code
        elif isinstance(error, HTTPException):
            code = f"HTTP_{error.status_code}"
        elif isinstance(error, asyncio.CancelledError | GeneratorExit | ClientDisconnect):
            code = _CLIENT_CLOSED
        else:
            code = "INTERNAL_ERROR"
        try:
            emit_agent_event(
                op_name=bridge.ASSISTANT_OP,
                identity={
                    "org": self.org,
                    "namespace": self.namespace or "-",
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
                slo_class=_slo_class(),
                budget_ms=effective_budget_ms(
                    bridge.ASSISTANT_OP, OP_BUDGETS_P99_MS[bridge.ASSISTANT_OP]
                ),
            )
        except Exception as e:  # noqa: BLE001 — telemetry never fails an answer
            log.debug("assistant answer: ledger row not written: %s", e)


def _slo_class() -> str:
    """The SLO class the op transport records for `assistant.ask`, so the route's rows
    and the op's fall in one."""
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME[bridge.ASSISTANT_OP]
    return sli.slo_class(op.name, writes=not op.read_only, admin=False)


class _Held:
    """What one admitted answer holds until its response ends: its per-replica slot, one
    of the org's in-flight slots, and its row."""

    def __init__(self, ticket: bridge.Ticket, row: _Row) -> None:
        self.ticket, self.row = ticket, row
        self.answer = bridge.Answer()
        self.finished = False  # the stream reached its end, or its error event
        self.slots = contextlib.ExitStack()
        self.slots.enter_context(tenant_slots.hold(ticket.org))

    def close(self, error: BaseException | None) -> None:
        self.ticket.release()
        self.slots.close()
        # The answer's id from the answer itself: a streamed body runs in a task of its
        # own, whose context this row does not see.
        set_ledger_run(self.answer.run_id)
        self.row.write(error)


class _AnswerStream(StreamingResponse):
    """A streamed answer whose holds are given back however the response ends: its
    `__call__` runs in the request's task, so a client that leaves before the first
    chunk, which never starts the body, still releases them."""

    def __init__(self, body: AsyncIterator[bytes], held: _Held) -> None:
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Request-Id": held.row.request_id},
        )
        self.held = held

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as e:  # a disconnect cancels the response, or ends it
            error = e
            raise
        finally:
            try:
                # Ends the body however the response ended, so its charge lands on the row.
                await self.body_iterator.aclose()  # type: ignore[attr-defined]
            finally:
                if error is None and not self.held.finished:
                    # The response returned with the stream unfinished: the client left
                    # (below ASGI 2.4 Starlette absorbs the disconnect).
                    error = asyncio.CancelledError()
                self.held.close(error)


@router.post("/v1/assistant/chat/completions")
async def chat_completions(request: Request) -> Any:
    """One answer from the Loom assistant. Body: OpenAI's chat request (`messages`,
    `stream`); any `model`, `conversation_id`, system message, tool or file is ignored.
    The caller needs `viewer` on the namespace."""
    bearer = _bearer(request)
    row = _Row(bearer, bearer.org, request.headers.get("X-Loom-Namespace") or "")
    try:
        try:
            auth = effective_auth(bearer, request.headers.get("X-Loom-On-Behalf-Of") or "")
        except PermissionError as e:
            raise _refusal(e) from e
        set_current_auth(auth)
        row.auth = auth
        try:
            payload = await request.json()
        except ValueError as e:
            raise ValidationError("the body must be JSON") from e
        if not isinstance(payload, dict):
            raise ValidationError("the body must be a JSON object")
        org = auth.org
        namespace = row.namespace or await default_namespace(auth, org)
        if not namespace:
            raise ValidationError("name a namespace (X-Loom-Namespace)")
        row.org, row.namespace = org, namespace
        messages = bridge.admitted_messages(payload.get("messages"))
        await check_acted_for(auth, org)
        await bridge.authorize(auth, org, namespace)
        # Drawn once the caller is authorized and before any cost check, as the op
        # transport draws them.
        admit_route(bridge.ASSISTANT_OP, namespace, org=org, actor=auth.subject)
        ticket = await bridge.open_answer(auth, org, namespace)
    except (LoomServerError, HTTPException, PermissionError) as e:
        refused = _refusal(e)
        row.write(refused)  # a refused answer leaves its row, as a refused op does
        raise refused from e
    except Exception as e:
        row.write(e)  # INTERNAL_ERROR, as the op transport records it
        raise
    try:
        held = _Held(ticket, row)
    except BaseException as e:
        ticket.release()
        row.write(e)
        raise
    try:
        await note_restricted_access(
            auth, op=bridge.ASSISTANT_OP, org=org, namespace=namespace, transport="http"
        )
    except BaseException as e:
        held.close(e)
        raise
    chunks = bridge.stream(ticket, messages, held.answer)
    if payload.get("stream"):
        return _AnswerStream(_streamed(chunks, held), held)
    return await _whole(chunks, held)


async def _streamed(chunks: AsyncIterator[bytes], held: _Held) -> AsyncIterator[bytes]:
    try:
        async for chunk in chunks:
            yield chunk
    except LoomServerError as e:
        # The status is sent: the refusal goes out as an error event, and on the row.
        set_ledger_run(held.answer.run_id)
        held.row.write(e)
        held.finished = True
        yield bridge.error_event(e)
        return
    finally:
        await chunks.aclose()  # type: ignore[attr-defined]
    held.finished = True


async def _whole(chunks: AsyncIterator[bytes], held: _Held) -> JSONResponse:
    answer = held.answer
    error: BaseException | None = None
    try:
        async for _ in chunks:
            pass
    except BaseException as e:
        error = e
        raise
    finally:
        held.close(error)
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
        headers={"X-Request-Id": held.row.request_id},
    )


def attach(app: Any) -> None:
    app.include_router(router)
