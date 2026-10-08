"""Unit coverage for the synchronous SDK facade (loom.sync.SyncAgent).

Server-free: the underlying transport.post is patched with a fake coroutine,
so these exercise the loop-thread marshalling, concurrent calls (ClickGap's
ThreadPoolExecutor pattern), idempotency-key passthrough, attribute parity
with the async client, and the close() lifecycle — without a live control
plane.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from loom import SyncAgent
from loom.sync import _SyncClientProxy


@pytest.fixture
def fake_calls() -> list[dict]:
    return []


@pytest.fixture
def sa(fake_calls: list[dict]):
    agent = SyncAgent(org="clickhouse", namespace="clickgap", agent="analyst")

    async def fake_post(op_path, payload, idempotency_key=None, *, timeout_s=None):
        # Yield so the loop genuinely round-trips through the scheduler.
        await asyncio.sleep(0)
        fake_calls.append(
            {
                "op": op_path,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "loop_thread": threading.get_ident(),
            }
        )
        if op_path == "/v1/memory.get":
            return {"found": True, "memory_key": payload["memory_key"], "value": "v"}
        if op_path == "/v1/memory.search":
            return {
                "results": [],
                "abstain": True,
                "abstain_reason": "top hit similarity 0.19 is below the abstain threshold 0.30",
                "abstain_code": "below_threshold",
                "retrieval_risk": "abstain",
            }
        if op_path == "/v1/memory.bulk_set":
            return {"written": [{"memory_key": "k", "version": 1}], "skipped_locked": []}
        return {"version": 1, "memory_id": "m", "content_sha256": "x"}

    # Patch at the transport layer so every client method round-trips through it.
    agent._agent.transport.post = fake_post  # type: ignore[method-assign]
    try:
        yield agent
    finally:
        agent.close()


def test_blocking_call_returns_value(sa: SyncAgent, fake_calls: list[dict]) -> None:
    got = sa.memory.get("last_pr")
    assert got is not None
    assert got["memory_key"] == "last_pr"
    assert fake_calls[-1]["op"] == "/v1/memory.get"


def test_search_round_trips(sa: SyncAgent) -> None:
    res = sa.memory.search("flaky test", top_k=20)
    # MemorySearchResult is a list subclass carrying the abstain envelope.
    assert list(res) == []
    assert res.abstain is True
    assert res.retrieval_risk == "abstain"
    # `abstain_code` is the bounded field callers should branch on. The prose
    # reason interpolates the similarity, so it is not a stable branch key —
    # a client parsing it would break on every distinct score.
    assert res.abstain_code == "below_threshold"
    assert "0.19" in res.abstain_reason


def test_bulk_set_passes_idempotency_key(sa: SyncAgent, fake_calls: list[dict]) -> None:
    res = sa.memory.bulk_set(
        [{"memory_key": "k", "value": "v", "memory_type": "observation"}],
        idempotency_key="chunk-deadbeef",
    )
    assert res == {"written": [{"memory_key": "k", "version": 1}], "skipped_locked": []}
    assert fake_calls[-1]["idempotency_key"] == "chunk-deadbeef"


def test_methods_run_on_one_shared_loop_thread(sa: SyncAgent, fake_calls: list[dict]) -> None:
    sa.memory.get("a")
    sa.memory.search("b")
    sa.memory.get("c")
    loop_threads = {c["loop_thread"] for c in fake_calls}
    # All coroutines executed on the single private loop thread, and it is
    # NOT the calling (main) thread.
    assert len(loop_threads) == 1
    assert threading.get_ident() not in loop_threads


def test_concurrent_calls_from_many_threads(sa: SyncAgent) -> None:
    # Mirrors ClickGap's finding-processor: up to N parallel recalls against
    # one shared SyncAgent. All must complete correctly.
    def _call(i: int) -> str:
        return sa.memory.get(f"k{i}")["memory_key"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_call, range(64)))
    assert results == [f"k{i}" for i in range(64)]


def test_coroutine_methods_are_wrapped_blocking(sa: SyncAgent) -> None:
    # The proxied method is a plain (non-coroutine) callable, not a coroutine
    # function — calling it blocks and returns the value directly.
    assert not inspect.iscoroutinefunction(sa.memory.get)
    assert callable(sa.memory.get)


def test_non_coroutine_attributes_pass_through(sa: SyncAgent) -> None:
    # _identity is a regular helper on MemoryClient — it must pass through
    # untouched (same bound method as the underlying async client's).
    proxy = sa.memory
    assert isinstance(proxy, _SyncClientProxy)
    ident = proxy._identity(None, None)
    assert ident["org"] == "clickhouse"
    assert ident["namespace"] == "clickgap"


def test_close_is_idempotent(sa: SyncAgent) -> None:
    sa.close()
    sa.close()  # second close must be a no-op, not an error


def test_calls_after_close_raise() -> None:
    agent = SyncAgent(org="clickhouse", namespace="clickgap", agent="analyst")
    agent.close()
    with pytest.raises(RuntimeError, match="closed"):
        agent.memory.get("k")


def test_context_manager_closes() -> None:
    with SyncAgent(org="clickhouse", namespace="clickgap", agent="analyst") as agent:
        assert agent.org == "clickhouse"
    assert agent._closed is True


def _live_loop_threads() -> int:
    return sum(t.name == "loom-sync-loop" and t.is_alive() for t in threading.enumerate())


def test_a_raising_with_block_completes_the_ambient_run_failed() -> None:
    import json

    import httpx

    sent: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((req.url.path, json.loads(req.content or b"{}")))
        return httpx.Response(200, json={"run_id": "amb", "results": []})

    sa = SyncAgent("o", "n", "bot", api_key="k", base_url="http://x", auto_run=True)

    async def swap() -> None:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )

    sa._run(swap())
    with pytest.raises(ValueError), sa:
        sa.memory.search("q")
        raise ValueError("boom")
    done = [b for p, b in sent if p == "/v1/run.complete"]
    assert [(d["status"], d["outcome"]) for d in done] == [("failed", "negative")]


def test_racing_closers_close_once_without_error() -> None:
    for _ in range(5):
        agent = SyncAgent(org="clickhouse", namespace="clickgap", agent="analyst")
        errors: list[BaseException] = []

        def close(a: SyncAgent = agent, errs: list[BaseException] = errors) -> None:
            try:
                a.close()
            except BaseException as e:  # noqa: BLE001 — collected for the assertion
                errs.append(e)

        threads = [threading.Thread(target=close) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [] and agent._closed


def test_an_unclosed_agent_is_collected_and_releases_its_loop_thread() -> None:
    import gc
    import time
    import weakref

    before = _live_loop_threads()
    agent = SyncAgent(org="clickhouse", namespace="clickgap", agent="analyst")
    ref = weakref.ref(agent)
    del agent
    gc.collect()
    deadline = time.monotonic() + 5
    while _live_loop_threads() > before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ref() is None and _live_loop_threads() == before


def test_close_from_the_loop_thread_raises_at_once() -> None:
    import time

    agent = SyncAgent(org="clickhouse", namespace="clickgap", agent="analyst")

    async def close_on_loop() -> None:
        agent.close()

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="loop thread"):
        agent._run(close_on_loop())
    assert time.monotonic() - t0 < 1
    agent.close()
    assert agent._closed
