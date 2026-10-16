"""From the third `memory.set` inside one 200 ms window, an
Agent's sets travel as one `memory.bulk_set` (the same four verdicts).
Server-free: a fake transport records what goes out."""

from __future__ import annotations

import asyncio
from typing import Any

import loom.memory
import pytest
from loom import Agent
from loom.errors import RateLimited
from loom.memory import COALESCE_MAX_BATCH, _bulk_verdict


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_bulk: BaseException | None = None
        self.bulk_extra: dict[str, Any] = {}
        self.row_verdicts: dict[str, str] = {}

    async def post(
        self, op_path: str, payload: dict[str, Any], idempotency_key=None, *, timeout_s=None
    ) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        await asyncio.sleep(0)
        if op_path == "/v1/memory.set":
            return {
                "version": 7,
                "memory_id": "id-" + payload["memory_key"],
                "memory_key": payload["memory_key"],
            }
        if op_path == "/v1/memory.bulk_set":
            if self.fail_bulk is not None:
                raise self.fail_bulk
            return {
                "written": [
                    {
                        "memory_key": e["memory_key"],
                        "version": 0
                        if e["memory_key"] in self.bulk_extra.get("merged", [])
                        else 100 + i,
                        "conflict_verdict": self.row_verdicts.get(e["memory_key"], ""),
                    }
                    for i, e in enumerate(payload["entries"])
                ],
                **self.bulk_extra,
            }
        return {}

    async def close(self) -> None:
        pass

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x")
    a.transport = _FakeTransport()  # type: ignore[assignment]
    return a


async def test_two_sets_go_out_as_two_point_writes(agent: Agent) -> None:
    t: Any = agent.transport
    r1, r2 = await asyncio.gather(agent.memory.set("a", 1), agent.memory.set("b", 2))
    assert t.paths() == ["/v1/memory.set", "/v1/memory.set"]
    assert r1["memory_id"] == "id-a" and "coalesced" not in r2


async def test_third_set_inside_the_window_starts_a_bulk(agent: Agent) -> None:
    t: Any = agent.transport
    t.bulk_extra = {"merged": ["d"], "conflicted": ["e"]}
    results = await asyncio.gather(*[agent.memory.set(k, {"v": k}) for k in "abcde"])
    assert sorted(t.paths()) == ["/v1/memory.bulk_set", "/v1/memory.set", "/v1/memory.set"]
    bulk = [p for path, p in t.calls if path == "/v1/memory.bulk_set"][0]
    assert [e["memory_key"] for e in bulk["entries"]] == ["c", "d", "e"]
    # Identity rides the bulk payload once, not on the entries.
    assert bulk["org"] == "o" and bulk["namespace"] == "n" and bulk["agent"] == "ag"
    assert all("org" not in e for e in bulk["entries"])
    by_key = {r["memory_key"]: r for r in results}
    assert by_key["a"]["version"] == 7 and "coalesced" not in by_key["a"]
    assert by_key["c"]["coalesced"] is True and by_key["c"]["version"] == 100
    assert by_key["c"]["conflict_verdict"] == "clear" and by_key["c"]["noop"] is False
    assert by_key["d"]["conflict_verdict"] == "merge" and by_key["d"]["noop"] is True
    assert by_key["e"]["conflict_verdict"] == "conflict"


async def test_a_coalesced_set_keeps_the_rows_own_verdict(agent: Agent) -> None:
    # `refines` rides only the per-row verdict; no aggregate list carries it.
    t: Any = agent.transport
    t.row_verdicts = {"c": "refines", "d": "conflict"}
    t.bulk_extra = {"conflicted": ["d", "e"]}
    results = await asyncio.gather(*[agent.memory.set(k, {"v": k}) for k in "abcde"])
    by_key = {r["memory_key"]: r["conflict_verdict"] for r in results if r.get("coalesced")}
    # c and d carry their own verdict; e has none, so the lists supply it.
    assert by_key == {"c": "refines", "d": "conflict", "e": "conflict"}


async def test_a_coalesced_set_of_a_blacklisted_value_raises_as_alone(agent: Agent) -> None:
    # Alone, memory.set answers 422 BLACKLISTED; coalesced, the same error is raised.
    from loom.errors import ValidationError

    t: Any = agent.transport
    t.row_verdicts = {"d": "blacklisted"}
    results = await asyncio.gather(
        *[agent.memory.set(k, {"v": k}) for k in "abcde"], return_exceptions=True
    )
    (refused,) = [r for r in results if isinstance(r, BaseException)]
    assert isinstance(refused, ValidationError) and refused.code == "BLACKLISTED"
    assert refused.status == 422


async def test_a_set_with_point_only_fields_is_never_coalesced(agent: Agent) -> None:
    t: Any = agent.transport
    await asyncio.gather(
        agent.memory.set("a", 1),
        agent.memory.set("b", 2),
        agent.memory.set("c", 3, if_match_sha256="abc"),
        agent.memory.set("d", 4, on_expire="tombstone"),
        agent.memory.set("e", 5),
        agent.memory.set("f", 6),
    )
    sets = [p for path, p in t.calls if path == "/v1/memory.set"]
    assert {p["memory_key"] for p in sets} == {"a", "b", "c", "d"}
    bulk = [p for path, p in t.calls if path == "/v1/memory.bulk_set"]
    assert len(bulk) == 1 and [e["memory_key"] for e in bulk[0]["entries"]] == ["e", "f"]


async def test_different_identities_keep_separate_windows(agent: Agent) -> None:
    t: Any = agent.transport
    await asyncio.gather(
        *[agent.memory.set(f"u1-{i}", i, user_id="u1") for i in range(3)],
        *[agent.memory.set(f"u2-{i}", i, user_id="u2") for i in range(3)],
    )
    # Each identity: two point writes, then a one-entry remainder that goes
    # out as a point write too (a bulk of one is not worth a bulk).
    assert t.paths().count("/v1/memory.set") == 6
    assert "/v1/memory.bulk_set" not in t.paths()


async def test_coalescing_off_sends_every_set_alone() -> None:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x", coalesce_sets=False)
    t = _FakeTransport()
    a.transport = t  # type: ignore[assignment]
    await asyncio.gather(*[a.memory.set(f"k{i}", i) for i in range(6)])
    assert t.paths() == ["/v1/memory.set"] * 6


async def test_a_failed_bulk_fails_every_set_it_carried(agent: Agent) -> None:
    t: Any = agent.transport
    t.fail_bulk = RateLimited(retry_after_s=3)
    outs = await asyncio.gather(
        *[agent.memory.set(f"k{i}", i) for i in range(5)], return_exceptions=True
    )
    assert [isinstance(o, RateLimited) for o in outs] == [False, False, True, True, True]


async def test_window_expiry_resets_the_burst_count(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.memory.set("a", 1)
    await agent.memory.set("b", 2)
    await asyncio.sleep(0.25)
    await agent.memory.set("c", 3)  # a new window: goes out at once
    assert t.paths() == ["/v1/memory.set"] * 3


async def test_flush_sets_sends_the_gathered_batch_now(agent: Agent) -> None:
    t: Any = agent.transport
    tasks = [asyncio.ensure_future(agent.memory.set(f"k{i}", i)) for i in range(5)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "/v1/memory.bulk_set" not in t.paths()  # the timer has not fired
    await agent.memory.flush_sets()
    assert "/v1/memory.bulk_set" in t.paths()
    results = await asyncio.gather(*tasks)
    assert sum(1 for r in results if r.get("coalesced")) == 3


async def test_close_flushes_before_the_pool_closes(agent: Agent) -> None:
    t: Any = agent.transport
    tasks = [asyncio.ensure_future(agent.memory.set(f"k{i}", i)) for i in range(4)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await agent.close()
    results = await asyncio.gather(*tasks)
    assert len(results) == 4 and "/v1/memory.bulk_set" in t.paths()


async def test_a_batch_is_capped_at_the_verdict_cap(agent: Agent, monkeypatch: Any) -> None:
    # A window long enough that a slow runner cannot roll it over mid-burst
    # (that split the burst into two partial batches, neither at the cap);
    # the burst is exactly two point writes plus one full batch, so the cap
    # closes it and no timer is waited on.
    monkeypatch.setattr(loom.memory, "COALESCE_WINDOW_S", 30.0)
    t: Any = agent.transport
    n = COALESCE_MAX_BATCH + 2
    results = await asyncio.gather(*[agent.memory.set(f"k{i}", i) for i in range(n)])
    bulks = [p for path, p in t.calls if path == "/v1/memory.bulk_set"]
    assert max(len(b["entries"]) for b in bulks) == COALESCE_MAX_BATCH
    assert sum(len(b["entries"]) for b in bulks) + t.paths().count("/v1/memory.set") == n
    assert len(results) == n


def test_bulk_verdict_spelling_matches_the_point_path() -> None:
    res = {"merged": ["a"], "rejected_pinned": ["b"], "duplicates": ["c"], "conflicted": ["d"]}
    assert [_bulk_verdict(k, res) for k in "abcde"] == [
        "merge",
        "rejected",
        "duplicate",
        "conflict",
        "clear",
    ]


def test_agent_and_datapack_default_to_coalescing() -> None:
    from loom import Datapack

    assert Agent(org="o", namespace="n", agent="a", api_key="x").coalesce_sets is True
    assert Datapack("o", "n", api_key="x").agent("a").coalesce_sets is True
    assert Datapack("o", "n", api_key="x", coalesce_sets=False).agent("a").coalesce_sets is False
