"""Typing fixture: code a consumer writes against the SDK
that must type-check under mypy --strict. Never executed."""

from __future__ import annotations

import asyncio

from loom import Agent, CallResult, Datapack, RetryPolicy, SyncAgent
from loom.errors import NotFound, RateLimited
from loom.ops import OPS, MemoryGetInput, MemorySearchBody, MemorySearchInput, spec


async def main() -> None:
    agent = Agent("acme", "support", "bot", api_key="k", retry=RetryPolicy(max_attempts=2))
    body: MemorySearchBody = {"query": "deploy window", "top_k": 5}  # identity stamped by call()
    full: MemorySearchInput = {"org": "acme", "namespace": "support", "query": "deploy window"}
    out: dict[str, object] = await agent.call("memory.search", body, user_id="u-42")
    meta: CallResult = await agent.call_with_meta("memory.search", body, retry={"max_attempts": 1})
    rid: str = meta.request_id
    probe = await agent.probe(ops=["memory.ask"], fields={"memory.search": ["exclude_keys"]})
    ok: bool = bool(probe["ok"])
    get_body: MemoryGetInput = {
        "org": "acme",
        "namespace": "support",
        "agent": "bot",
        "memory_key": "k",
    }
    idem: bool = OPS["memory.search"].idempotent
    s = spec("memory.get")
    if s is not None:
        _role: str = s.role
    try:
        await agent.memory.get("k")
    except NotFound as e:
        _code: str = e.code
        _rid: str = e.request_id
    except RateLimited as e:
        _after: float = e.retry_after_s
    await agent.memory.set("k", "v", tags=["t"], ttl_seconds=60, on_expire="tombstone")
    got = await agent.memory.get("k", fields=["value"])
    if got is not None:
        _value: object = got.get("value")
    hits = await agent.memory.search("deploy window", top_k=3, tags=["ops"])
    pack = Datapack("acme", "support", api_key="k")
    bot = pack.agent("bot")
    await bot.close()
    with SyncAgent("acme", "support", "bot", api_key="k") as sa:
        _sync_out: dict[str, object] = sa.call("memory.search", body)
        _sync_meta: CallResult = sa.call_with_meta("memory.search", body)
    await agent.close()
    _ = (out, rid, ok, get_body, full, idem, hits)


asyncio.run(main())
