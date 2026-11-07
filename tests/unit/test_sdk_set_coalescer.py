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
from loom.memory import COALESCE_MAX_BATCH


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_bulk: BaseException | None = None
        self.bulk_extra: dict[str, Any] = {}
        self.row_verdicts: dict[str, str] = {}
        # A verdict for one entry by its value, when one key comes twice.
        self.value_verdicts: dict[str, str] = {}
        # Values the server skips as already stored (version 0).
        self.unchanged_values: set[str] = set()
        # More fields on one entry's result row, by key.
        self.row_extra: dict[str, dict[str, Any]] = {}
        # Values that get no result row at all, as N-1 answers a blacklisted entry.
        self.dropped_values: set[str] = set()

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
                        or str(e["value"]) in self.unchanged_values
                        else 100 + i,
                        "conflict_verdict": self.value_verdicts.get(
                            str(e["value"]), self.row_verdicts.get(e["memory_key"], "")
                        ),
                        **self.row_extra.get(e["memory_key"], {}),
                    }
                    for i, e in enumerate(payload["entries"])
                    if str(e["value"]) not in self.dropped_values
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
    t.row_verdicts = {"d": "merge", "e": "conflict"}
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
    # c and d carry their own verdict; e has none, so it is clear whatever the lists say.
    assert by_key == {"c": "refines", "d": "conflict", "e": "clear"}


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


async def test_a_coalesced_set_the_blacklist_could_not_check_is_sent_alone(agent: Agent) -> None:
    # The batch's values together were past the blacklist's budget: that entry goes out
    # as a memory.set of its own and its caller gets that answer, never a silent no-op.
    t: Any = agent.transport
    t.row_verdicts = {"d": "blacklist_unchecked"}
    results = await asyncio.gather(*[agent.memory.set(k, {"v": k}) for k in "abcde"])
    assert t.paths()[-1] == "/v1/memory.set" and t.calls[-1][1]["memory_key"] == "d"
    alone = results["abcde".index("d")]
    assert alone["version"] == 7 and "coalesced" not in alone


async def test_a_coalesced_set_against_a_pinned_row_raises_as_alone(agent: Agent) -> None:
    # Alone, memory.set answers 409 PINNED_CONFLICT; coalesced, the same error is raised.
    from loom.errors import PreconditionFailed

    t: Any = agent.transport
    t.row_verdicts = {"d": "rejected"}
    results = await asyncio.gather(
        *[agent.memory.set(k, {"v": k}) for k in "abcde"], return_exceptions=True
    )
    (refused,) = [r for r in results if isinstance(r, BaseException)]
    assert isinstance(refused, PreconditionFailed) and refused.code == "PINNED_CONFLICT"
    assert refused.status == 409


@pytest.mark.parametrize(
    ("skipped", "verdict", "code", "status"),
    [
        ("skipped_pinned", "pinned", "PINNED_CONFLICT", 409),
        ("skipped_locked", "locked", "LOCKED", 423),
    ],
)
async def test_a_coalesced_set_to_a_pinned_or_locked_key_raises_as_alone(
    agent: Agent, skipped: str, verdict: str, code: str, status: int
) -> None:
    # Alone, a set to a pinned key answers 409 and to a read_only key 423; the
    # batch skips the entry, gives it that verdict and names it in its skipped list.
    from loom.errors import LoomError

    t: Any = agent.transport
    t.row_verdicts = {"d": verdict}
    t.bulk_extra = {"merged": ["d"], skipped: ["d"]}
    results = await asyncio.gather(
        *[agent.memory.set(k, {"v": k}) for k in "abcde"], return_exceptions=True
    )
    (refused,) = [r for r in results if isinstance(r, BaseException)]
    assert isinstance(refused, LoomError) and refused.code == code
    assert refused.status == status


@pytest.mark.parametrize(
    ("verdict", "extra", "point_keys", "peer_ids"),
    [
        # memory.set alone, to a pinned key (server/memory/app/write.py).
        (
            "pinned",
            {"pinned_blocker_id": "X"},
            {"code", "message", "pinned_blocker_id", "peer_ids", "memory_key"},
            ["X"],
        ),
        # memory.set alone, close to a pinned row (server/memory/app/set_commit.py).
        (
            "rejected",
            {"pinned_blocker_id": "X", "peers": [{"memory_id": "X"}, {"memory_id": "Y"}]},
            {"code", "message", "pinned_blocker_id", "peer_ids"},
            ["X", "Y"],
        ),
    ],
)
async def test_a_coalesced_pinned_refusal_carries_the_point_writes_detail(
    agent: Agent, verdict: str, extra: dict[str, Any], point_keys: set[str], peer_ids: list[str]
) -> None:
    from loom.errors import PreconditionFailed

    t: Any = agent.transport
    t.row_verdicts = {"d": verdict}
    t.row_extra = {"d": extra}
    results = await asyncio.gather(
        *[agent.memory.set(k, {"v": k}) for k in "abcde"], return_exceptions=True
    )
    (refused,) = [r for r in results if isinstance(r, BaseException)]
    assert isinstance(refused, PreconditionFailed)
    assert point_keys <= set(refused.detail)
    assert refused.detail["pinned_blocker_id"] == "X"
    assert refused.detail["peer_ids"] == peer_ids
    assert refused.detail["memory_key"] == "d"


async def test_a_restatement_of_a_pinned_key_in_a_burst_with_a_change_resolves(
    agent: Agent,
) -> None:
    # The pinned key k is set to its own value V, then to W, in one batch: the
    # server skips V as unchanged and refuses only W, though the list names k.
    from loom.errors import PreconditionFailed

    t: Any = agent.transport
    t.value_verdicts = {"W": "pinned"}
    t.bulk_extra = {"skipped_pinned": ["k"]}
    a, b, same, changed = await asyncio.gather(
        agent.memory.set("a", 1),
        agent.memory.set("b", 2),
        agent.memory.set("k", "V"),
        agent.memory.set("k", "W"),
        return_exceptions=True,
    )
    assert same["memory_key"] == "k" and same["coalesced"] is True
    assert isinstance(changed, PreconditionFailed) and changed.code == "PINNED_CONFLICT"


async def test_a_restatement_does_not_take_a_rejected_siblings_verdict(agent: Agent) -> None:
    # k is set to its stored value V, then to W, which is close to a pinned row
    # under another key. The server skips V with no verdict and rejects W, and
    # rejected_pinned names k; only W raises, as each set alone would.
    from loom.errors import PreconditionFailed

    t: Any = agent.transport
    t.value_verdicts = {"V": "", "W": "rejected"}
    t.unchanged_values = {"V"}
    t.bulk_extra = {"rejected_pinned": ["k"]}
    a, b, same, changed = await asyncio.gather(
        agent.memory.set("a", 1),
        agent.memory.set("b", 2),
        agent.memory.set("k", "V"),
        agent.memory.set("k", "W"),
        return_exceptions=True,
    )
    assert isinstance(same, dict) and same["conflict_verdict"] == "clear" and same["noop"] is True
    assert isinstance(changed, PreconditionFailed) and changed.code == "PINNED_CONFLICT"


async def test_a_missing_result_row_shifts_no_other_sets_result(agent: Agent) -> None:
    # An N-1 server leaves a blacklisted entry out of `written`: the sets after it still
    # get their own rows, and the one left out gets the version-0 placeholder.
    t: Any = agent.transport
    t.dropped_values = {"D"}
    results = await asyncio.gather(*[agent.memory.set(k, k.upper()) for k in "abcde"])
    by_key = {r["memory_key"]: r for r in results if r.get("coalesced")}
    assert by_key["c"]["version"] == 100
    assert by_key["d"]["version"] == 0 and by_key["d"]["noop"] is True
    assert by_key["e"]["version"] == 102


async def test_repeated_keys_in_one_batch_take_their_rows_in_order(agent: Agent) -> None:
    t: Any = agent.transport
    t.dropped_values = {"D"}
    results = await asyncio.gather(
        agent.memory.set("a", 1),
        agent.memory.set("b", 2),
        agent.memory.set("k", "V"),
        agent.memory.set("d", "D"),
        agent.memory.set("k", "W"),
    )
    assert [r["version"] for r in results[2:]] == [100, 0, 102]


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


def test_agent_and_datapack_default_to_coalescing() -> None:
    from loom import Datapack

    assert Agent(org="o", namespace="n", agent="a", api_key="x").coalesce_sets is True
    assert Datapack("o", "n", api_key="x").agent("a").coalesce_sets is True
    assert Datapack("o", "n", api_key="x", coalesce_sets=False).agent("a").coalesce_sets is False
