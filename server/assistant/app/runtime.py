"""The native assistant: one bounded answer under its caller's delegated identity.

A request is admitted only when the caller may read the namespace, the org makes model
calls, the namespace's assistant share of its daily model cost cap is not spent, and this
web process runs fewer than its limit of answers there. The answer runs under a token
delegated from the caller's for that answer alone (`viewer` on the namespace, consumer
`loom-assistant`, ended with the caller's token, subject or sign-in session),
presented to Loom on every native tool call (docs/architecture/assistant.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from server.assistant.api.models import AssistantCitation
from server.assistant.app import agent, citations, evidence, service
from server.assistant.domain.limits import MAX_HISTORY, MAX_MESSAGE_CHARS
from server.assistant.domain.steps import steps_of
from server.assistant.domain.support import UNCHECKED, CallRow
from server.auth import fail_closed, sharing
from server.auth.answer_tokens import ANSWER_JTI_PREFIX, is_answer_token
from server.auth.authz import CachedGate, check_authorized
from server.auth.delegation import delegate
from server.auth.directory import directory
from server.auth.jwt import AuthInfo, new_jti
from server.auth.session_protection import protected_for
from server.core import request_tail, request_work, shielded
from server.core.ch import set_query_tag
from server.core.diagnostics import private_work
from server.core.errors import (
    AssistantSelfCallError,
    AssistantUnavailableError,
    AssistantUpstreamError,
    AuthInsufficientError,
    BackendUnavailableError,
    LoomServerError,
    OverloadedError,
    RateLimitedError,
    ValidationError,
    classified,
)
from server.identity.domain.read_scope import request_read_narrowed
from server.knowledge.app import source_access
from server.llm.customer import ModelCallsDisabled, switched_off
from server.observability.budget import operation_over_share
from server.observability.consumer import ASSISTANT_CONSUMER
from server.observability.run_context import set_ledger_run
from server.operations.base import Role
from server.settings import settings

log = logging.getLogger(__name__)

ASSISTANT_OP = "assistant.ask"
# What a request may carry, as assistant.ask's input model bounds it: its history and
# the question.
_MAX_MESSAGES = MAX_HISTORY + 1
# A refused share check (the cap is spent) is retried after this: the cap is a rolling day.
_CAP_RETRY_S = 3600  # not a setting: a hint; the cap's own window is the setting

# The answers this web process runs per (org, namespace), by slot id: a replica with
# several web workers runs that many times LOOM_ASSISTANT_CONCURRENT_ANSWERS.
_running: dict[tuple[str, str], set[int]] = {}
_slot_ids = itertools.count(1)


@dataclass
class Ticket:
    """An admitted answer: where it runs, the token it reads under, and its slot."""

    org: str
    namespace: str
    # The delegated token stays out of logs and assertion representations.
    token: str = field(repr=False)
    slot: int
    # Loom's line about the caller's credential (`asker_line`), the last line of the
    # question the native runtime is sent.
    asker: str
    mirrored: bool = False

    def release(self) -> None:
        """Give the answer's slot back; a second call does nothing."""
        _give_back(self.org, self.namespace, self.slot)


@dataclass
class Answer:
    """What one answer produced, filled while it streams."""

    mirrored: bool = False
    run_id: str = ""
    text: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    state: agent.State | None = None
    # Each citation checked against this answer's structured tool receipts.
    citations: list[AssistantCitation] = field(default_factory=list)
    # How far the answer rests on returned evidence, once it ended.
    support: str = UNCHECKED
    # The op and LOOM_* names it wrote that the build neither serves nor documents.
    unknown_names: list[str] = field(default_factory=list)


def admitted_messages(messages: Any) -> list[dict[str, str]]:
    """The caller's `user` and `assistant` messages as text, in order. A `system`
    message would override the agent's instructions and a tool message is the native runtime's
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
            "an org-wide token cannot ask the assistant: an answer reads one org",
            reason="org_wide",
        )


def refuse_self_call(auth: AuthInfo) -> None:
    """ASSISTANT_SELF_CALL for an answer's own token (consumer `loom-assistant`) or any
    token delegated from one: calls go one way, so an answer never asks the assistant."""
    if is_answer_token(auth):
        raise AssistantSelfCallError("an assistant answer's token cannot ask the assistant")


async def authorize(auth: AuthInfo, org: str, namespace: str) -> None:
    """The refusals that need no model spend read: no assistant here, a token that may
    not ask, a caller without `viewer`. A caller's rate-limit buckets are drawn after
    these, and the cost checks after them (`open_answer`)."""
    if not settings.assistant_enabled:
        raise AssistantUnavailableError("the native assistant is disabled", reason="not_configured")
    refuse_org_wide(auth)
    refuse_self_call(auth)
    if request_read_narrowed(auth):
        raise AuthInsufficientError(
            "a token whose reads are narrower than its namespace cannot ask the assistant",
            reason="narrowed",
        )
    if auth.expires_at is not None and not (auth.session_id and auth.first_party):
        left = max(auth.expires_at - int(time.time()), 0)
        if left < int(settings.assistant_answer_ttl_s):
            raise AuthInsufficientError(
                f"the token expires in {left} s, sooner than an answer may run "
                f"({int(settings.assistant_answer_ttl_s)} s)",
                reason="token_expires_soon",
                left_s=left,
            )
    try:
        await check_authorized(auth, Role.VIEWER, org, namespace)
    except PermissionError as e:
        if isinstance(e, LoomServerError):
            raise
        log.debug("assistant refused: %s", e)
        raise AuthInsufficientError(f"the caller cannot read {namespace!r} in {org!r}") from e


async def asker_line(auth: AuthInfo, org: str, namespace: str) -> str:
    """One line, in fixed words, of what the caller asks with: the kind of credential, the
    role the gate gives it here and the kind of namespace. The agent reads it to answer
    "how do I…" for this caller; it carries no subject or name, and grants nothing (the
    answer's token decides every read). Run once an answer is admitted, it never refuses
    one: a check that cannot run gives the role as unknown."""
    try:
        role = await _gate_role(auth, org, namespace)
        place = _place(org, namespace)
    except Exception as e:  # noqa: BLE001 — the line never refuses an answer
        log.warning("the assistant could not tell the caller's role: %s", e)
        role, place = "unknown", "namespace"
    return (
        f"Loom, for how-to answers only: this question comes with {_credential(auth)}, "
        f"whose role in this {place} is {role}."
    )


def _credential(auth: AuthInfo) -> str:
    """What decides the caller's role, most specific first: a dashboard or `loom login`
    session, a token capped by the grant of the person who made it, a token whose claim
    names its role per namespace, or another token."""
    if auth.session_id and auth.first_party:
        return "a sign-in session"
    if auth.creator:
        return "a delegated token"
    if auth.has_ns_roles():
        return "a scoped token"
    return "a token"


async def _gate_role(auth: AuthInfo, org: str, namespace: str) -> str:
    """The highest role the gate gives the caller here, from the cached grant maps the
    listings use: viewer already passed; `unknown` when a check was refused because a
    read it needed failed, not for the role."""
    gate = CachedGate(auth, org)
    for role in (Role.ADMIN, Role.WRITER):
        if await gate.admits(namespace, role):
            return role.name.lower()
        if fail_closed.unreadable():
            return "unknown"
    return "viewer"


def _place(org: str, namespace: str) -> str:
    """The kind of namespace, which decides who can change a role in it: by name for a
    collection, then the directory's cached classification (no read)."""
    if sharing.is_personal(namespace):
        return "personal collection"
    if sharing.is_collection(namespace):
        return "collection"
    if directory().is_restricted_cached(org, namespace):
        return "restricted namespace"
    return "namespace"


async def _afford(org: str, namespace: str) -> None:
    """Refuse unconfigured models, a disabled org or an unreadable or spent share."""
    reason = service.configuration_error()
    if reason:
        raise AssistantUnavailableError("the native assistant cannot answer", reason=reason)
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


async def answer_token(auth: AuthInfo, org: str, namespace: str, *, mirrored: bool = False) -> str:
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
    from server.auth.reader_identity import reader_subject

    reader = reader_subject(auth)
    return delegated.mint(
        auth.subject,
        consumer=ASSISTANT_CONSUMER,
        jti=f"{ANSWER_JTI_PREFIX}{new_jti()}",
        reads_as_creator=bool(reader and reader == delegated.creator),
        session_protection=protected_for(auth),
        answer_mirrors=mirrored and protected_for(auth),
    )


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
    answer's slot, its token and its asker line, run before anything streams, so a
    refused request is an HTTP error and pays for no line. The stream gives the slot
    back; so does `Ticket.release` on a refusal after this."""
    mirrored = await _mirror_admission(org, namespace)
    with private_work() if mirrored else contextlib.nullcontext():
        await _afford(org, namespace)
        slot = _take_slot(org, namespace)
        try:
            token = await answer_token(auth, org, namespace, mirrored=mirrored)
            asker = await asker_line(auth, org, namespace)
        except BaseException:
            _give_back(org, namespace, slot)
            raise
    return Ticket(org, namespace, token, slot, asker, mirrored)


async def _mirror_admission(org: str, namespace: str) -> bool:
    """Fix the answer's privacy before model work, without reading private evidence."""
    if not settings.auth_actually_required():
        return False  # mirror reads require authenticated serving
    set_query_tag("assistant.mirror.admission")
    try:
        sources = await source_access.has_active_mirrored_sources(org, namespace)
    except Exception:  # noqa: BLE001 — unknown admission must never become an ordinary answer
        request_work.mark_mirrored(unread=True)
        with private_work():
            pass
        raise BackendUnavailableError(
            "the assistant's source access could not be checked"
        ) from None
    if not sources:
        return False
    if not request_work.mark_mirrored():
        raise BackendUnavailableError("the assistant's source access could not be checked")
    # Install diagnostic filters before the marked answer's first further await.
    with private_work():
        pass
    return True


def _give_back(org: str, namespace: str, slot: int) -> None:
    held = _running.get((org, namespace), set())
    held.discard(slot)
    if not held:
        _running.pop((org, namespace), None)


def _event(obj: str, run_id: str, **fields: Any) -> bytes:
    return (
        "data: " + json.dumps({"object": obj, "id": run_id, "choices": [], **fields}) + "\n\n"
    ).encode()


def _steps(answer: Answer) -> list[dict[str, Any]]:
    receipts = answer.state.receipts if answer.state else []
    steps, _ = steps_of([CallRow(receipt.op, True) for receipt in receipts])
    return [asdict(step) for step in steps]


async def stream(
    ticket: Ticket,
    messages: list[dict[str, str]],
    answer: Answer,
    *,
    live: bool = False,
) -> AsyncIterator[bytes]:
    """Provisional native output, then checked citations and the terminal marker."""
    answer.mirrored = ticket.mirrored
    if ticket.mirrored:
        yield b'data: {"mirrored":true}\n\n'
    chunks = _stream(ticket, messages, answer, live=live)
    try:
        while True:
            with private_work() if ticket.mirrored else contextlib.nullcontext():
                try:
                    chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
            yield chunk
    finally:
        with private_work() if ticket.mirrored else contextlib.nullcontext():
            if ticket.mirrored:
                await _close_owned(chunks.aclose(), name="assistant.stream.producer")
            else:
                await chunks.aclose()


async def _close_owned(action: Awaitable[None], *, name: str) -> None:
    async def bounded() -> None:
        async with asyncio.timeout(request_tail.REQUIRED_STEP_TIMEOUT_S):
            await action

    await shielded.to_its_end(bounded(), name=name)


async def _stream(
    ticket: Ticket,
    messages: list[dict[str, str]],
    answer: Answer,
    *,
    live: bool,
) -> AsyncGenerator[bytes]:
    state = agent.State()
    answer.state, answer.run_id = state, state.run_id
    set_ledger_run(state.run_id)
    source = agent.run(ticket.org, ticket.namespace, ticket.token, ticket.asker, messages, state)
    try:
        async with asyncio.timeout(float(settings.assistant_answer_ttl_s)):
            async with contextlib.aclosing(source):
                async for frame in source:
                    if live and frame.get("object") in ("loom.context", "loom.tool"):
                        yield _event("loom.step", state.run_id, steps=_steps(answer))
                    for choice in frame.get("choices", []):
                        text = (choice.get("delta") or {}).get("content")
                        if isinstance(text, str):
                            answer.text.append(text)
                    if sum(map(len, answer.text)) > settings.assistant_max_output_chars:
                        raise AssistantUpstreamError("the answer exceeded its output bound")
                    answer.prompt_tokens, answer.completion_tokens = (
                        state.input_tokens,
                        state.output_tokens,
                    )
                    yield ("data: " + json.dumps(frame) + "\n\n").encode()
            answer.prompt_tokens, answer.completion_tokens = state.input_tokens, state.output_tokens
            async with contextlib.aclosing(_ended(ticket, answer, live=live)) as ended:
                async for chunk in ended:
                    yield chunk
    except TimeoutError as e:
        raise AssistantUpstreamError("the answer exceeded its total deadline") from e
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        raise AssistantUpstreamError(
            "the native assistant received an invalid provider response"
        ) from e
    finally:
        ticket.release()


async def _ended(ticket: Ticket, answer: Answer, *, live: bool) -> AsyncGenerator[bytes]:
    """The stream's last events once the native runtime's ended: `loom.text_done` (`live` only), the
    checks' `loom.citations`, then `[DONE]`."""
    try:
        if live:
            yield _event("loom.text_done", answer.run_id)
        yield await _checked(ticket, answer, live=live)
        yield b"data: [DONE]\n\n"
    finally:
        # The checks give the slot back before their event goes out, so the next question
        # never finds it held; this covers a caller that leaves at `loom.text_done`.
        ticket.release()


async def _checked(ticket: Ticket, answer: Answer, *, live: bool = False) -> bytes:
    """Verify request-owned receipts inside the answer's slot, then emit citations,
    support and accumulated tool steps. Release the slot after these checks."""
    try:
        text = "".join(answer.text)
        receipts = answer.state.receipts if answer.state else []
        answer.citations = evidence.verify(text, receipts)
        answer.unknown_names = await citations.unknown_names(text)
        if any(not c.verified for c in answer.citations) or answer.unknown_names:
            raise AssistantUpstreamError("the answer contains an unverified citation or name")
        answer.support = evidence.support(text, receipts, answer.citations)
        steps = {"steps": _steps(answer)}

    finally:
        ticket.release()
    return _event(
        "loom.citations",
        answer.run_id,
        citations=[c.model_dump() for c in answer.citations],
        support=answer.support,
        unknown_names=answer.unknown_names,
        **steps,
    )


def error_event(e: LoomServerError) -> bytes:
    """the native runtime's failure once the stream has begun, as the error event OpenAI's streams
    send, with the `category` and `retryable` every coded error body carries and the HTTP
    status it would have had, so a client maps it as it maps that response."""
    detail = {**classified(e.to_detail(), e.status_code), "status": e.status_code}
    return f"data: {json.dumps({'error': detail})}\n\n".encode()


async def ask(auth: AuthInfo, org: str, namespace: str, messages: Any) -> Answer:
    """One whole answer, for `assistant.ask`."""
    kept = admitted_messages(messages)
    await authorize(auth, org, namespace)
    ticket = await open_answer(auth, org, namespace)
    answer = Answer(mirrored=ticket.mirrored)
    with request_tail.publication_group() as publication:
        try:
            async for _ in stream(ticket, kept, answer):
                pass
        finally:
            # A failed charge also releases the slot; a second release does nothing.
            ticket.release()
        publication.commit()
    return answer


__all__ = [
    "ANSWER_JTI_PREFIX",
    "ASSISTANT_OP",
    "Answer",
    "Ticket",
    "authorize",
    "refuse_org_wide",
    "refuse_self_call",
    "admitted_messages",
    "answer_token",
    "ask",
    "error_event",
    "open_answer",
    "stream",
]
