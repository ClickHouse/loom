"""The assistant bridge: one answer from the agent Loom seeded in LibreChat, as its caller.

A request is admitted only when the caller may read the namespace, the org makes model
calls, the namespace's assistant share of its daily model cost cap is not spent, and this
web process runs fewer than its limit of answers there. The answer runs under a token
delegated from the caller's for that answer alone (`viewer` on the namespace, consumer
`loom-assistant`, ended with the caller's token, subject or sign-in session), which
LibreChat sends back to Loom on every tool call (docs/architecture/assistant.md).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, NoReturn

import httpx

from server.assistant.api.models import MAX_HISTORY, MAX_MESSAGE_CHARS
from server.assistant.app import service
from server.auth.authz import check_authorized
from server.auth.delegation import delegate
from server.auth.jwt import AuthInfo
from server.core.errors import (
    AssistantUnavailableError,
    AssistantUpstreamError,
    AuthInsufficientError,
    BackendUnavailableError,
    LoomServerError,
    OverloadedError,
    RateLimitedError,
    ValidationError,
    error_kind,
)
from server.identity.domain.read_scope import request_read_narrowed
from server.llm.customer import ModelCallsDisabled, switched_off
from server.observability import cost_accumulator
from server.observability.budget import operation_over_share
from server.observability.consumer import ASSISTANT_CONSUMER
from server.observability.run_context import clean_run_id, set_ledger_run
from server.operations.base import Role
from server.settings import settings

log = logging.getLogger(__name__)

ASSISTANT_OP = "assistant.ask"
# What a request may carry, as assistant.ask's input model bounds it: its history and
# the question.
_MAX_MESSAGES = MAX_HISTORY + 1
# A refused share check (the cap is spent) is retried after this: the cap is a rolling day.
_CAP_RETRY_S = 3600  # not a setting: a hint; the cap's own window is the setting
# Seconds to reach LibreChat; an answer then runs up to LOOM_ASSISTANT_ANSWER_TTL_S.
_CONNECT_S = 10.0  # not a setting: LibreChat is on the same network

# The answers this web process runs per (org, namespace), by slot id: a replica with
# several web workers runs that many times LOOM_ASSISTANT_CONCURRENT_ANSWERS.
_running: dict[tuple[str, str], set[int]] = {}
_slot_ids = itertools.count(1)


@dataclass
class Ticket:
    """An admitted answer: where it runs, the token it reads under, and its slot."""

    org: str
    namespace: str
    agent_id: str
    # Secrets: kept out of the repr, so no log line or assertion diff prints them.
    api_key: str = field(repr=False)
    token: str = field(repr=False)
    slot: int

    def release(self) -> None:
        """Give the answer's slot back; a second call does nothing."""
        _give_back(self.org, self.namespace, self.slot)


@dataclass
class Answer:
    """What one answer produced, filled while it streams."""

    run_id: str = ""
    text: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_seen: bool = False
    # LibreChat's stream began: from here the answer is charged.
    started: bool = False
    error: str = ""


def admitted_messages(messages: Any) -> list[dict[str, str]]:
    """The caller's `user` and `assistant` messages as text, in order. A `system`
    message would override the agent's instructions and a tool message is LibreChat's
    own, so both are dropped, as are files and images; the last message is the
    question."""
    if not isinstance(messages, list) or len(messages) > _MAX_MESSAGES:
        raise ValidationError(f"messages must be a list of at most {_MAX_MESSAGES}")
    kept: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):  # OpenAI content parts: keep the text ones
            content = "\n".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            if len(content) > MAX_MESSAGE_CHARS:
                raise ValidationError(f"a message is at most {MAX_MESSAGE_CHARS} characters")
            kept.append({"role": str(m["role"]), "content": content})
    if not kept or kept[-1]["role"] != "user":
        raise ValidationError("the last message must be the caller's question")
    return kept


def refuse_org_wide(auth: AuthInfo) -> None:
    """An org-wide (`*`) token cannot ask: an answer reads one org."""
    if auth.org == "*":
        raise AuthInsufficientError(
            "an org-wide token cannot ask the assistant: an answer reads one org"
        )


async def authorize(auth: AuthInfo, org: str, namespace: str) -> None:
    """The refusals that need no model spend read: no assistant here, a token that may
    not ask, a caller without `viewer`. A caller's rate-limit buckets are drawn after
    these, and the cost checks after them (`open_answer`)."""
    if not settings.assistant_librechat_url:
        raise AssistantUnavailableError(
            "no LibreChat is configured for the assistant (LOOM_ASSISTANT_LIBRECHAT_URL)"
        )
    refuse_org_wide(auth)
    if request_read_narrowed(auth):
        raise AuthInsufficientError(
            "a token whose reads are narrower than its namespace cannot ask the assistant"
        )
    if auth.expires_at is not None and not (auth.session_id and auth.first_party):
        left = auth.expires_at - int(time.time())
        if left < int(settings.assistant_answer_ttl_s):
            raise AuthInsufficientError(
                f"the token expires in {max(left, 0)} s, sooner than an answer may run "
                f"({int(settings.assistant_answer_ttl_s)} s)"
            )
    try:
        await check_authorized(auth, Role.VIEWER, org, namespace)
    except PermissionError as e:
        if isinstance(e, LoomServerError):
            raise
        log.debug("assistant refused: %s", e)
        raise AuthInsufficientError(f"the caller cannot read {namespace!r} in {org!r}") from e


async def _afford(org: str, namespace: str) -> tuple[str, str]:
    """The org makes model calls and the assistant's share of the namespace's daily cap
    is not spent; (agent id, API key) when so."""
    answering = service.answering()
    if answering is None:
        raise AssistantUnavailableError(
            "the assistant is not set up on this replica yet: /v1/health names why"
        )
    if await switched_off(org):
        raise ModelCallsDisabled(f"model calls are switched off for org {org!r}")
    share = float(settings.assistant_budget_share)
    try:
        over = await operation_over_share(org, namespace, operation=ASSISTANT_OP, share=share)
    except Exception as e:  # any failed read refuses: an unreadable spend is never under the cap
        log.warning("reading the assistant's spend in %s/%s failed: %s", org, namespace, e)
        raise BackendUnavailableError(
            f"the assistant's spend in {namespace!r} could not be read: ask again shortly",
            operation=ASSISTANT_OP,
        ) from e
    if over:
        raise RateLimitedError(
            f"the assistant's share of {namespace!r}'s daily model cost cap is spent",
            retry_after_s=_CAP_RETRY_S,
        )
    return answering


async def answer_token(auth: AuthInfo, org: str, namespace: str) -> str:
    """The token one answer's tool calls read under: delegated from the caller's,
    `viewer` on the namespace alone, no longer-lived than an answer may run, and ended
    with the caller's token, subject or sign-in session."""
    delegated = await delegate(
        auth,
        org=org,
        requested={namespace: "viewer"},
        scoped=True,
        ttl_seconds=int(settings.assistant_answer_ttl_s),
        is_service_account=auth.is_service_account,
        ends_with_session=True,
    )
    return delegated.mint(auth.subject, consumer=ASSISTANT_CONSUMER)


def _take_slot(org: str, namespace: str) -> int:
    """One of this web process's answers in the namespace, or OVERLOADED."""
    held = _running.setdefault((org, namespace), set())
    if len(held) >= int(settings.assistant_concurrent_answers):
        raise OverloadedError(
            f"the assistant is already answering {len(held)} questions in {namespace!r}",
            retry_after_s=5,
        )
    slot = next(_slot_ids)
    held.add(slot)
    return slot


async def open_answer(auth: AuthInfo, org: str, namespace: str) -> Ticket:
    """After `authorize` and the caller's rate-limit buckets: the cost refusals, the
    answer's slot and its token, run before anything streams, so a refused request is an
    HTTP error. The stream gives the slot back; so does `Ticket.release` on a refusal
    after this."""
    agent_id, api_key = await _afford(org, namespace)
    slot = _take_slot(org, namespace)
    try:
        token = await answer_token(auth, org, namespace)
    except BaseException:
        _give_back(org, namespace, slot)
        raise
    return Ticket(org, namespace, agent_id, api_key, token, slot)


def _give_back(org: str, namespace: str, slot: int) -> None:
    held = _running.get((org, namespace), set())
    held.discard(slot)
    if not held:
        _running.pop((org, namespace), None)


async def stream(
    ticket: Ticket, messages: list[dict[str, str]], answer: Answer
) -> AsyncIterator[bytes]:
    """The admitted answer as LibreChat's agents API streams it (OpenAI chat-completion
    chunks as server-sent events), with `answer` filled as it goes, for at most
    LOOM_ASSISTANT_ANSWER_TTL_S. LibreChat unreachable, refusing, ending the answer with
    an error or running past the limit raises ASSISTANT_UPSTREAM_FAILED, naming none of
    LibreChat's text. Once LibreChat's stream has begun, the answer's spend is charged
    however it ends."""
    body = {
        "model": ticket.agent_id,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        # The answer's token: the agent's MCP server sends it back as its bearer.
        "parent_message_id": ticket.token,
    }
    headers = {"Authorization": f"Bearer {ticket.api_key}", "Accept-Encoding": "identity"}
    ttl = float(settings.assistant_answer_ttl_s)
    timeout = httpx.Timeout(ttl, connect=_CONNECT_S)
    deadline = asyncio.get_running_loop().time() + ttl
    try:
        async with httpx.AsyncClient(
            base_url=str(settings.assistant_librechat_url), timeout=timeout
        ) as client:
            async with asyncio.timeout_at(deadline):
                r = await client.send(
                    client.build_request(
                        "POST", "/api/agents/v1/chat/completions", json=body, headers=headers
                    ),
                    stream=True,
                )
            try:
                if r.status_code != 200:
                    # Its status says it all: the body is not read, so no stalled, endless
                    # or broken body holds the answer, and no secret it echoes is logged.
                    _refused(ticket, r.status_code)
                answer.started = True
                lines = r.aiter_lines()
                while True:
                    # The deadline bounds each read from LibreChat, never a send to the
                    # caller: nothing is yielded inside it.
                    async with asyncio.timeout_at(deadline):
                        try:
                            line = await anext(lines)
                        except StopAsyncIteration:
                            break
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    _note(answer, line[6:])
                    if answer.error:
                        log.warning(
                            "LibreChat ended an answer with an error: %s",
                            _redacted(ticket, answer.error),
                        )
                        raise AssistantUpstreamError("LibreChat ended the answer with an error")
                    yield (line + "\n\n").encode()
            finally:
                await r.aclose()
    except httpx.HTTPError as e:
        log.warning("reaching LibreChat for an answer failed: %s", e)
        raise AssistantUpstreamError(f"LibreChat could not be reached ({error_kind(e)})") from e
    except TimeoutError as e:
        raise AssistantUpstreamError(f"the answer ran past {int(ttl)} s") from e
    finally:
        ticket.release()
        if answer.started:
            _charge(answer)
    yield b"data: [DONE]\n\n"


def _redacted(ticket: Ticket, text: str) -> str:
    """LibreChat's error text for a log line, without the answer's token or the service
    key it may echo back, cut after the redaction so no part of either survives."""
    out = text
    for secret, name in ((ticket.token, "<answer token>"), (ticket.api_key, "<service key>")):
        if secret:
            out = out.replace(secret, name)
    return out[:300]


def _refused(ticket: Ticket, status: int) -> NoReturn:
    """LibreChat refused the request. A key or agent it no longer knows (401, 403, 404)
    means the stored ones went stale: the service is set up again, unless another answer
    already did and this one only carried the key it replaced."""
    log.warning("LibreChat refused an answer (%s)", status)
    if status in (401, 403, 404):
        stored = service.answering()
        if stored is None or stored == (ticket.agent_id, ticket.api_key):
            # Dropped by this answer, or by another that met the same stale key first.
            if stored is not None:
                service.forget_answering(f"LibreChat refused the stored key or agent ({status})")
            raise AssistantUpstreamError(
                f"LibreChat refused the stored key or agent ({status}); this replica is "
                "setting the assistant up again: ask again shortly"
            )
        raise AssistantUpstreamError(
            f"LibreChat refused the key this answer carried ({status}), which this replica "
            "has already replaced: ask again"
        )
    raise AssistantUpstreamError(f"LibreChat refused the answer ({status})")


def _note(answer: Answer, data: str) -> None:
    """What one chunk tells: the answer's id (its ledger run), text, usage, an error."""
    try:
        chunk = json.loads(data)
    except ValueError:
        return
    if not isinstance(chunk, dict):
        return  # not a chunk: a LibreChat that misbehaves skips it
    if not answer.run_id and chunk.get("id"):
        answer.run_id = clean_run_id(chunk["id"])
        set_ledger_run(answer.run_id)
    if chunk.get("error"):
        # Whole: `_redacted` takes out any secret it echoes before it cuts the text.
        answer.error = json.dumps(chunk["error"])
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        try:
            answer.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            answer.completion_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            pass  # unreadable usage counts as none: the answer is charged the ceiling
        else:
            # Empty usage counts as none too: a begun answer is never free.
            answer.usage_seen = answer.prompt_tokens + answer.completion_tokens > 0
    choices = chunk.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str):
            answer.text.append(content)


def _charge(answer: Answer) -> None:
    """The answer's spend: its usage at the model's price, or the per-answer ceiling
    when the usage never arrived or the model has no price, so no answer is free."""
    model = str(service.current_definition().model)
    if answer.usage_seen and cost_accumulator.is_priced(model):
        cost_accumulator.report_llm(model, answer.prompt_tokens, answer.completion_tokens)
    else:
        cost_accumulator.report_charge(model, float(settings.assistant_answer_ceiling_usd))


def error_event(e: LoomServerError) -> bytes:
    """A refusal once the stream has begun, as the error event OpenAI's streams send."""
    return f"data: {json.dumps({'error': e.to_detail()})}\n\n".encode()


async def ask(auth: AuthInfo, org: str, namespace: str, messages: Any) -> Answer:
    """One whole answer, for `assistant.ask`."""
    kept = admitted_messages(messages)
    await authorize(auth, org, namespace)
    ticket = await open_answer(auth, org, namespace)
    answer = Answer()
    async for _ in stream(ticket, kept, answer):
        pass
    return answer


__all__ = [
    "ASSISTANT_OP",
    "Answer",
    "Ticket",
    "authorize",
    "refuse_org_wide",
    "admitted_messages",
    "answer_token",
    "ask",
    "error_event",
    "open_answer",
    "stream",
]
