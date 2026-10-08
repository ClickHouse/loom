"""The SDK names its consumer.

`Agent(consumer=...)` / `SyncAgent(consumer=...)` / `Datapack(consumer=...)`
hand the label to the `Transport`, which stamps it on every op body that
names none (absent, None, '' or the wire's sentinel `"-"`); a body that names
its own consumer wins; an Agent with no consumer sends every payload
byte-for-byte as before. The ambient run opens under the
Agent's consumer when it has one. `memory.search` takes `session_id` (a
session-named read is credited).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from loom import Agent, Datapack, SyncAgent
from loom.transport import CONSUMER_MAX_CHARS, Transport, consumer_label

# ---- a transport whose httpx client records what would go on the wire ------


class _Recorder:
    """Replaces the httpx.AsyncClient inside a real Transport, so the stamp
    is exercised on the real `post_with_meta` path (before_post, headers,
    the read label) and the recorded body is the wire body."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.reply = reply or {"ok": True, "results": []}
        self.headers: dict[str, str] = {}

    async def post(self, path: str, *, json: Any = None, headers: Any = None, **kw: Any) -> Any:
        import json as _json

        self.bodies.append(dict(json))
        self.raw.append(_json.dumps(json, sort_keys=True).encode())
        req = httpx.Request("POST", f"http://x{path}")
        return httpx.Response(200, json=self.reply, request=req)

    async def aclose(self) -> None:
        return None


def _wire(agent: Agent) -> _Recorder:
    rec = _Recorder()
    agent.transport._client = rec  # type: ignore[assignment]
    return rec


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---- the label -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("-", ""),
        (" clickgap ", "clickgap"),
        ("a\x00b\nc", "abc"),
        (True, ""),
        (7, "7"),
        ("x" * 100, "x" * CONSUMER_MAX_CHARS),
    ],
)
def test_consumer_label_normalises_like_the_server(raw: Any, want: str) -> None:
    assert consumer_label(raw) == want


def test_transport_with_consumer_rules() -> None:
    t = Transport(base_url="http://x", api_key="k", consumer=" clickgap ")
    assert t.consumer == "clickgap"
    assert t.with_consumer({"org": "o"}) == {"org": "o", "consumer": "clickgap"}
    assert t.with_consumer({"org": "o", "consumer": ""}) == {"org": "o", "consumer": "clickgap"}
    assert t.with_consumer({"org": "o", "consumer": "-"}) == {"org": "o", "consumer": "clickgap"}
    assert t.with_consumer({"org": "o", "consumer": None}) == {"org": "o", "consumer": "clickgap"}
    assert t.with_consumer({"org": "o", "consumer": "mine"}) == {"org": "o", "consumer": "mine"}
    # No consumer configured: the very same object comes back, nothing added.
    bare = Transport(base_url="http://x", api_key="k")
    assert bare.consumer == ""
    p = {"org": "o", "consumer": ""}
    assert bare.with_consumer(p) is p
    _run(t.close())
    _run(bare.close())


def test_transport_does_not_mutate_the_callers_dict() -> None:
    t = Transport(base_url="http://x", api_key="k", consumer="c")
    p = {"org": "o"}
    out = t.with_consumer(p)
    assert out == {"org": "o", "consumer": "c"} and p == {"org": "o"} and out is not p
    _run(t.close())


# ---- every op body the Agent posts -------------------------------------------


def test_agent_stamps_the_consumer_on_every_op_body() -> None:
    async def go() -> None:
        async with Agent(
            "o", "n", "ag", base_url="http://x", api_key="k", consumer="clickgap"
        ) as a:
            assert a.consumer == "clickgap" and a.transport.consumer == "clickgap"
            rec = _wire(a)
            await a.memory.search("q")  # hand-built payload, agent '*'
            await a.memory.set("k", "v")  # write path
            await a.call("memory.list", {"limit": 3})  # generic call
            await a.code.grep("needle")
            await a.run.begin(goal="g")  # run client sends consumer=''
            await a.probe()  # ops.capabilities posts {}
            assert [b.get("consumer") for b in rec.bodies] == ["clickgap"] * 6
            # A caller naming its own consumer wins over the Agent's.
            await a.call("code.grep", pattern="needle", consumer="hand-built")
            await a.call("memory.search", {"query": "q"}, consumer="other")
            assert rec.bodies[-2]["consumer"] == "hand-built"
            assert rec.bodies[-1]["consumer"] == "other"

    _run(go())


def test_agent_without_consumer_sends_unchanged_bodies() -> None:
    async def go() -> None:
        async with Agent("o", "n", "ag", base_url="http://x", api_key="k") as a:
            assert a.consumer == ""
            rec = _wire(a)
            await a.memory.search("q")
            await a.memory.set("k", "v")
            await a.call("memory.list", {"limit": 3})
            await a.code.grep("needle")
            await a.probe()
            assert "consumer" not in rec.bodies[0]
            assert "consumer" not in rec.bodies[1]
            assert "consumer" not in rec.bodies[2]
            assert "consumer" not in rec.bodies[3]
            assert rec.bodies[4] == {}

    _run(go())


def test_payload_is_byte_identical_with_and_without_the_feature_when_unset() -> None:
    """The stamp path is a no-op for a caller that sets nothing: the wire bytes
    of a run through `post_with_consumer` equal the body built by hand."""

    async def go() -> list[bytes]:
        async with Agent("o", "n", "ag", base_url="http://x", api_key="k") as a:
            rec = _wire(a)
            await a.memory.search("q", top_k=3)
            await a.memory.get("k")
            return rec.raw

    raw = _run(go())
    expected_search = {
        "org": "o",
        "namespace": "n",
        "agent": "*",
        "query": "q",
        "scope": "private",
        "top_k": 3,
        "search_mode": "rrf",
        "alpha": 0.5,
        "auto_touch": True,
    }
    got = json.loads(raw[0])
    for k, v in expected_search.items():
        assert got[k] == v
    assert "consumer" not in got
    assert "consumer" not in json.loads(raw[1])


def test_session_id_and_run_id_ride_the_search_body() -> None:
    async def go() -> None:
        async with Agent("o", "n", "ag", base_url="http://x", api_key="k", consumer="c") as a:
            rec = _wire(a)
            await a.memory.search("q")
            assert "session_id" not in rec.bodies[0] and "run_id" not in rec.bodies[0]
            await a.memory.search("q", session_id="s1", run_id="r1", agent_id="ag")
            b = rec.bodies[1]
            assert b["session_id"] == "s1" and b["run_id"] == "r1" and b["agent"] == "ag"
            assert b["consumer"] == "c"

    _run(go())


def test_ambient_run_begins_under_the_agents_consumer() -> None:
    async def go() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        named = Agent(
            "o", "n", "ag", base_url="http://x", api_key="k", consumer="prod", auto_run=True
        )
        named.transport._client = _Recorder({"run_id": "r-1"})  # type: ignore[assignment]
        await named.memory.search("q")
        await named.close()
        plain = Agent("o", "n", "ag", base_url="http://x", api_key="k", auto_run=True)
        plain.transport._client = _Recorder({"run_id": "r-2"})  # type: ignore[assignment]
        await plain.memory.search("q")
        await plain.close()
        return named.transport._client.bodies, plain.transport._client.bodies  # type: ignore[attr-defined]

    named_bodies, plain_bodies = _run(go())
    begin = next(b for b in named_bodies if "goal" in b)
    assert begin["consumer"] == "prod"
    begin_plain = next(b for b in plain_bodies if "goal" in b)
    assert begin_plain["consumer"] == "ag"  # the agent-label fallback


# ---- SyncAgent and Datapack ----------------------------------------------------


def test_sync_agent_and_datapack_pass_the_consumer_through() -> None:
    with SyncAgent("o", "n", "ag", base_url="http://x", api_key="k", consumer="clickgap") as sa:
        assert sa.consumer == "clickgap"
        rec = _Recorder()
        sa.transport._client = rec
        sa.memory.search("q")
        sa.call("memory.list", {"limit": 1})
        assert [b["consumer"] for b in rec.bodies] == ["clickgap", "clickgap"]
    with SyncAgent("o", "n", "ag", base_url="http://x", api_key="k") as sa2:
        assert sa2.consumer == ""

    pack = Datapack("o", "n", base_url="http://x", api_key="k", consumer="pack-product")
    a = pack.agent("ag")
    b = pack.agent("bg", consumer="override")
    c = Datapack("o", "n", base_url="http://x", api_key="k").agent("cg")
    try:
        assert a.consumer == "pack-product" and b.consumer == "override" and c.consumer == ""
    finally:
        _run(a.close())
        _run(b.close())
        _run(c.close())
