"""Session automatic buffers retain the durable protection of their producers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from loom import Agent
from loom.errors import BackendUnavailable
from loom.session_protection import AutomaticCaptureSuppressed, SessionProtection

TURN = {"role": "user", "content": "example turn"}


@pytest_asyncio.fixture
async def world(tmp_path):
    family = SessionProtection.create(base=tmp_path / "family")
    seen, replies = [], {}

    async def respond(request):
        op = request.url.path.removeprefix("/v1/")
        seen.append((op, json.loads(request.content)))
        reply = replies.get(op, {})
        if callable(reply):
            reply = await reply(request)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    agent = Agent(
        org="example",
        namespace="notes",
        agent="example",
        api_key="test-token",
        base_url="http://inert.invalid",
        session_protection=family,
        retry={"max_attempts": 1},
        pacing=False,
        coalesce_sets=False,
        auto_run=False,
        mentor_brief=False,
    )
    agent.transport._client._transport = httpx.MockTransport(respond)
    try:
        yield SimpleNamespace(agent=agent, family=family, seen=seen, replies=replies)
    finally:
        await agent.transport.close()


async def test_clean_transcript_extraction_and_carry_over_keep_identity(world):
    session = world.agent.session("thread", user_id="reader", run_id="run", flush_every=1)
    await session.append(TURN)
    await session.append({"role": "assistant", "content": "next"})
    assert [op for op, _ in world.seen] == [
        "conversation.create",
        "conversation.append",
        "memory.set_from_messages",
        "conversation.append",
        "memory.set_from_messages",
    ]
    assert world.seen[-1][1]["last_messages"] == [TURN]
    assert all(
        body["session_id"] == "thread" and body["user_id"] == "reader" and body["run_id"] == "run"
        for _, body in world.seen
    )


async def test_first_marked_pack_returns_and_skips_optional_history(world):
    world.replies["context.pack"] = {"mirrored": True, "rendered": "private context"}
    session = world.agent.session("thread")
    result = await session.resume()
    assert result.system == "private context" and result.messages == []
    assert world.family.protected
    assert [op for op, _ in world.seen] == ["context.pack"]
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.history()


async def test_first_marked_history_returns_before_next_automatic_action(world):
    world.replies["conversation.get"] = {"mirrored": True, "found": True, "messages": [TURN]}
    session = world.agent.session("thread")
    assert await session.history() == [TURN]
    assert world.family.protected
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.append(TURN)
    assert len(world.seen) == 1


async def test_preexisting_mark_discards_buffer_and_carry_over(world):
    session = world.agent.session("thread", transcript=False, flush_every=0)
    await session.append(TURN)
    await session.flush()
    await session.append(TURN)
    assert session._last_flushed == [TURN]
    world.family.mark()
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.flush()
    assert session._pending == session._last_flushed == []
    assert len(world.seen) == 1


async def test_retained_producer_blocks_later_flush_outside_its_context(world, tmp_path):
    producer = SessionProtection.create(base=tmp_path / "producer")
    session = world.agent.session("thread", transcript=False, flush_every=0)
    with producer.automatic():
        await session.append(TURN)
    producer.mark()
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.flush()
    assert session._pending == session._last_flushed == []
    assert not world.seen and not world.family.protected


async def test_rebinding_agent_root_does_not_reset_session_protection(world, tmp_path):
    session = world.agent.session("thread", transcript=False, flush_every=0)
    await session.append(TURN)
    world.agent.transport.session_protection = SessionProtection.create(base=tmp_path / "new")
    world.family.mark()
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.flush()
    assert not world.seen and session.buffered == []


async def test_marked_append_reply_stops_remaining_turns_and_extraction(world):
    world.replies["conversation.append"] = {"mirrored": True, "stored": True}
    session = world.agent.session("thread", flush_every=1)
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.append(TURN, {"role": "assistant", "content": "unsent"})
    assert [op for op, _ in world.seen] == ["conversation.create", "conversation.append"]
    assert session._pending == session._last_flushed == []


@pytest.mark.parametrize("marked_error", [False, True])
async def test_mark_during_extraction_clears_without_requeue(world, marked_error):
    async def reply(request):
        await asyncio.sleep(0)
        if marked_error:
            return httpx.Response(500, json={"mirrored": True, "detail": "unavailable"})
        world.family.mark()
        return {"written": []}

    world.replies["memory.set_from_messages"] = reply
    session = world.agent.session("thread", transcript=False, flush_every=0)
    await session.append(TURN)
    with pytest.raises(BackendUnavailable if marked_error else AutomaticCaptureSuppressed):
        await session.flush()
    assert session._pending == session._last_flushed == []
    assert await session.flush() is None
    assert len(world.seen) == 1


async def test_clean_failure_requeues_old_batch_before_concurrent_append(world):
    session = world.agent.session("thread", transcript=False, flush_every=0)
    await session.append(TURN)
    newer = {"role": "user", "content": "newer"}

    async def failure(request):
        await session.append(newer)
        return httpx.Response(500, json={"detail": "unavailable"})

    world.replies["memory.set_from_messages"] = failure
    with pytest.raises(BackendUnavailable):
        await session.flush()
    assert session.buffered == [TURN, newer]
    world.replies["memory.set_from_messages"] = {"written": []}
    assert await session.flush() == {"written": []}
    assert world.seen[-1][1]["messages"] == [TURN, newer]


async def test_marked_cancellation_clears_carry_over_and_preserves_cancellation(world):
    session = world.agent.session("thread", transcript=False, flush_every=0)
    await session.append(TURN)
    await session.flush()
    await session.append(TURN)

    async def cancel(request):
        world.family.mark()
        raise asyncio.CancelledError()

    world.replies["memory.set_from_messages"] = cancel
    with pytest.raises(asyncio.CancelledError):
        await session.flush()
    assert session._pending == session._last_flushed == []


async def test_context_exit_keeps_primary_exception_and_discards_tainted_text(world):
    session = world.agent.session("thread", transcript=False, flush_every=0)
    with pytest.raises(RuntimeError, match="body failure"):
        async with session:
            await session.append(TURN)
            world.family.mark()
            raise RuntimeError("body failure")
    assert session._pending == session._last_flushed == [] and not world.seen


async def test_explicit_session_actions_remain_available_after_protection(world):
    session = world.agent.session("thread", user_id="reader")
    world.family.mark()
    await session.offload("explicit log")
    await session.fetch("explicit-blob")
    await session.note("explicit note")
    assert [op for op, _ in world.seen] == [
        "context.offload",
        "context.fetch",
        "working_memory.append",
    ]
    assert all(
        body["session_id"] == "thread" and body["user_id"] == "reader" for _, body in world.seen
    )


@pytest.mark.parametrize("reply_kind", ["success", "error", "malformed"])
async def test_unbound_explicit_response_observes_retained_producer(world, tmp_path, reply_kind):
    from loom.session_protection import SessionProtectionUnavailable

    world.agent.transport.session_protection = None
    producer = SessionProtection.create(base=tmp_path / "producer")
    session = world.agent.session("thread", transcript=False, flush_every=0)
    with producer.automatic():
        await session.append(TURN)
    reply = {
        "success": httpx.Response(200, json={"mirrored": True, "content": "private"}),
        "error": httpx.Response(500, json={"mirrored": True, "detail": "unavailable"}),
        "malformed": httpx.Response(200, content=b'{"mirrored":'),
    }[reply_kind]
    world.replies["context.fetch"] = reply
    if reply_kind == "success":
        assert (await session.fetch("blob"))["content"] == "private"
    else:
        with pytest.raises(
            BackendUnavailable if reply_kind == "error" else SessionProtectionUnavailable
        ):
            await session.fetch("blob")
    assert producer.protected and len(world.seen) == 1
    assert session._pending == session._last_flushed == []
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.append(TURN)
    assert await session.flush() is None


async def test_datapack_agents_share_the_declared_family(tmp_path):
    from loom import Datapack

    family = SessionProtection.create(base=tmp_path / "shared")
    pack = Datapack(
        "example",
        "notes",
        base_url="http://inert.invalid",
        api_key="test-token",
        session_protection=family,
        auto_run=False,
        mentor_brief=False,
    )
    first, second = pack.agent("first"), pack.agent("second")
    try:
        assert first.transport.session_protection is second.transport.session_protection is family
        session = first.session("thread", transcript=False, flush_every=0)
        await session.append(TURN)
        second.transport._client._transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"mirrored": True, "answer": "private"})
        )
        assert (await second.call("knowledge.get", {"address": "example"}))["answer"] == "private"
        first.transport._client._transport = httpx.MockTransport(
            lambda request: pytest.fail("protected buffered text dispatched")
        )
        with pytest.raises(AutomaticCaptureSuppressed):
            await session.flush()
    finally:
        await first.transport.close()
        await second.transport.close()


def test_sync_session_keeps_protection_across_its_loop_thread(tmp_path):
    from loom import SyncAgent

    family = SessionProtection.create(base=tmp_path / "shared")
    seen = []
    with SyncAgent(
        base_url="http://inert.invalid",
        api_key="test-token",
        session_protection=family,
        auto_run=False,
        mentor_brief=False,
        coalesce_sets=False,
        pacing=False,
    ) as agent:
        agent._agent.transport._client._transport = httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200, json={})
        )
        session = agent.session("thread", transcript=False, flush_every=0)
        session.append(TURN)
        family.mark()
        with pytest.raises(AutomaticCaptureSuppressed):
            session.flush()
        assert session.buffered == []
        session.note("explicit")
    assert [request.url.path for request in seen] == ["/v1/working_memory.append"]


async def test_protection_during_optional_history_authentication_keeps_completed_pack(world):
    class Auth(httpx.Auth):
        async def async_auth_flow(self, request):
            if request.url.path.endswith("conversation.get"):
                await asyncio.sleep(0)
                world.family.mark()
            yield request

    world.agent.transport._client.auth = Auth()
    world.replies["context.pack"] = {"rendered": "completed context"}
    session = world.agent.session("thread")
    result = await session.resume()
    assert result.system == "completed context" and result.messages == []
    assert world.family.protected
    assert [op for op, _ in world.seen] == ["context.pack"]
    with pytest.raises(AutomaticCaptureSuppressed):
        await session.append(TURN)
