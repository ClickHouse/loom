"""Deferred writes use the automatic protection of their actual producer."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from loom import Agent
from loom.memory import _SetCoalescer
from loom.session_protection import AutomaticCaptureSuppressed, SessionProtection
from loom.transport import Transport


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch):
    import loom.memory

    monkeypatch.setattr(loom.memory, "COALESCE_WINDOW_S", 3600)
    a = SessionProtection.create(base=tmp_path / "a")
    b = SessionProtection.create(base=tmp_path / "b")
    seen, bulk_reply = [], {}
    control = SimpleNamespace(bulk_started=asyncio.Event(), bulk_gate=None)

    async def respond(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("bulk_set"):
            control.bulk_started.set()
            if control.bulk_gate is not None:
                await control.bulk_gate.wait()
            return httpx.Response(
                200,
                json={
                    "written": [
                        {
                            "memory_key": entry["memory_key"],
                            "version": 1,
                            "conflict_verdict": "clear",
                        }
                        for entry in body["entries"]
                    ],
                    **bulk_reply,
                },
            )
        if request.url.path.endswith(("run.begin", "run.complete")):
            return httpx.Response(200, json={"run_id": "scope"})
        return httpx.Response(200, json={"version": 1})

    transport = Transport("http://inert.invalid", session_protection=b, pacing=False)
    transport._client._transport = httpx.MockTransport(respond)
    coalescer = _SetCoalescer(transport)
    try:
        yield SimpleNamespace(
            a=a,
            b=b,
            coalescer=coalescer,
            seen=seen,
            transport=transport,
            bulk_reply=bulk_reply,
            control=control,
            respond=respond,
        )
    finally:
        await coalescer.flush()
        await transport.close()


async def _group(world, name, root=None):
    with root.automatic() if root is not None else contextlib.nullcontext():
        tasks = [
            asyncio.create_task(
                world.coalescer.submit(
                    {
                        "org": "example",
                        "namespace": "notes",
                        "agent": "example",
                        "memory_key": f"{name}-{i}",
                        "value": f"{name}-{i}",
                    }
                )
            )
            for i in range(4)
        ]
        # Every producer reaches its first await before the caller changes protection.
        await asyncio.sleep(0)
    return tasks


def _sent_keys(world):
    return [
        entry["memory_key"]
        for path, body in world.seen
        for entry in (body["entries"] if path.endswith("bulk_set") else [body])
    ]


@pytest.mark.parametrize("first", ["explicit", "automatic"])
@pytest.mark.parametrize("flusher", ["explicit", "automatic"])
async def test_pending_producers_keep_their_own_context_at_manual_flush(world, first, flusher):
    automatic_first = first == "automatic"
    old = await _group(
        world, "auto" if automatic_first else "explicit", world.a if automatic_first else None
    )
    new = await _group(
        world, "explicit" if automatic_first else "auto", None if automatic_first else world.a
    )
    with world.a.automatic() if flusher == "automatic" else contextlib.nullcontext():
        world.a.mark()
        await world.coalescer.flush()
    results = await asyncio.gather(*old, *new, return_exceptions=True)
    automatic = results[:4] if automatic_first else results[4:]
    explicit = results[4:] if automatic_first else results[:4]
    assert all(isinstance(result, dict) for result in explicit)
    assert all(isinstance(result, AutomaticCaptureSuppressed) for result in automatic[2:])
    assert "auto-2" not in _sent_keys(world) and "auto-3" not in _sent_keys(world)
    assert set(f"explicit-{i}" for i in range(4)) <= set(_sent_keys(world))


async def test_independent_automatic_producer_survives_other_family_mark(world):
    old = await _group(world, "a", world.a)
    new = await _group(world, "b", world.b)
    world.a.mark()
    await world.coalescer.flush()
    results = await asyncio.gather(*old, *new, return_exceptions=True)
    assert all(isinstance(result, dict) for result in results[4:])
    assert all(isinstance(result, AutomaticCaptureSuppressed) for result in results[2:4])
    assert not world.b.protected


async def test_automatic_window_retains_original_bound_root_after_rebind(world, tmp_path):
    tasks = await _group(world, "auto", world.a)
    world.transport.session_protection = SessionProtection.create(base=tmp_path / "new")
    world.b.mark()
    await world.coalescer.flush()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, AutomaticCaptureSuppressed) for result in results[2:])
    assert "auto-2" not in _sent_keys(world) and not world.a.protected


@pytest.mark.parametrize("verdict", ["clear", "blacklist_unchecked"])
async def test_marked_bulk_stops_followup_and_does_not_complete_unsent_work(world, verdict):
    tasks = await _group(world, "auto", world.a)
    world.bulk_reply.update(
        mirrored=True,
        written=[
            {
                "memory_key": f"auto-{i}",
                "version": 0 if verdict == "blacklist_unchecked" else 1,
                "conflict_verdict": verdict,
            }
            for i in (2, 3)
        ],
    )
    await world.coalescer.flush()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, AutomaticCaptureSuppressed) for result in results[2:])
    assert [path for path, _ in world.seen] == [
        "/v1/memory.set",
        "/v1/memory.set",
        "/v1/memory.bulk_set",
    ]
    assert world.a.protected and world.b.protected


async def test_explicit_window_keeps_dispatch_and_results_on_protected_bound_agent(world):
    world.b.mark()
    tasks = await _group(world, "explicit")
    await world.coalescer.flush()
    assert all(result["version"] == 1 for result in await asyncio.gather(*tasks))
    assert set(_sent_keys(world)) == set(f"explicit-{i}" for i in range(4))


async def test_new_producer_waits_for_older_unchecked_retry_of_its_key(world):
    tasks = await _group(world, "old", world.a)
    world.control.bulk_gate = asyncio.Event()
    world.bulk_reply["written"] = [
        {"memory_key": "old-2", "version": 0, "conflict_verdict": "blacklist_unchecked"},
        {"memory_key": "old-3", "version": 1, "conflict_verdict": "clear"},
    ]
    newer = asyncio.create_task(
        world.coalescer.submit(
            {
                "org": "example",
                "namespace": "notes",
                "agent": "example",
                "memory_key": "old-2",
                "value": "new",
            }
        )
    )
    try:
        async with asyncio.timeout(1):
            await world.control.bulk_started.wait()
        assert not newer.done()
    finally:
        world.control.bulk_gate.set()
        await world.coalescer.flush()
        await asyncio.gather(*tasks, newer)
    assert [
        body["value"]
        for path, body in world.seen
        if path.endswith("memory.set") and body["memory_key"] == "old-2"
    ] == ["old-2", "new"]


async def test_deferred_dispatch_keeps_run_scope_after_its_producer_exits(world):
    agent = Agent(
        org="example",
        namespace="notes",
        agent="example",
        api_key="test-token",
        base_url="http://inert.invalid",
        session_protection=world.b,
        pacing=False,
        coalesce_sets=True,
        auto_run=True,
        mentor_brief=False,
    )
    agent.transport._client._transport = httpx.MockTransport(world.respond)
    try:
        async with agent.run_scope(goal="example"):
            with world.a.automatic():
                tasks = [
                    asyncio.create_task(agent.memory.set(f"key-{i}", f"value-{i}"))
                    for i in range(4)
                ]
                await asyncio.sleep(0)
        await agent.memory.flush_sets()
        assert all(result["version"] == 1 for result in await asyncio.gather(*tasks))
        assert sum(path.endswith("run.begin") for path, _ in world.seen) == 1
        assert all(
            body["run_id"] == "scope"
            for path, body in world.seen
            if path.endswith(("memory.set", "memory.bulk_set"))
        )
        assert agent.ambient_run_id == ""
    finally:
        await agent.close()
