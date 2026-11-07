"""A user erasure retires the consolidated summaries built from the user's memories,
though they live under another identity, and deletes the cached `memory.ask` answers
and the precedents that quoted them, tombstoning or purging; the residue census counts
the same rows. A summary of a key in a namespace where the user holds none stays, and
so do the answers that quoted it. In-process against a live ClickHouse; skips without
LOOM_CH_HOST."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.core.ch import get_client
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps
from server.erasure.store.predicates import UID
from server.memory.store.rows import build_embedding_row
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

_MEMORY_COLS = ["org", "namespace", "agent", "user_id", "session_id", "memory_key"]
_MEMORY_COLS += ["memory_type", "value", "version", "tags"]
_COUNTED = ("loom.memory", "loom.llm_response_cache", "loom.memory_ask_precedents")
# The user saga's steps this covers, in saga order.
_STEPS = (
    ("memory.tombstone", memory_steps.tombstone_memory_by_user),
    ("consolidated.tombstone", graph_steps.tombstone_consolidated_dependents_by_user),
    ("llm_response_cache.delete", graph_steps.delete_llm_response_cache_by_user),
    ("ask_precedents.delete", graph_steps.delete_ask_precedents_by_user),
    ("memory.retire", graph_steps.retire_memory_by_user),
)


async def _run(name: str, step: Any, org: str, ch: Any) -> None:
    with erasure_mutations.step_scope(name):
        await step(org, "alice", ch=ch)


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


def _seed(ch_client: Any, org: str) -> None:
    def memory(ns: str, user_id: str, key: str, value: str, tags: list[str], v: int = 1) -> None:
        ch_client.insert(
            "loom.memory",
            [[org, ns, "a", user_id, "-", key, "fact", value, v, tags]],
            column_names=_MEMORY_COLS,
        )

    memory("ns", "alice", "user_diet", "alice is vegetarian", [])
    memory("ns", "bob", "bob-diet", "bob eats fish", [])
    # the same extractor key, another user's fact, in another namespace
    memory("ns2", "carol", "user_diet", "carol is vegan", [])
    # summaries written under '-': one folded alice's row, the others did not
    consolidated = ["consolidated", "src:user_diet", "src:bob-diet"]
    memory("ns", "-", "topic-diets", "alice is vegetarian; bob eats fish", consolidated)
    memory("ns", "-", "topic-bob", "bob eats fish", ["consolidated", "src:bob-diet"])
    # rebuilt under the same key from bob's fact alone: the newer version stays
    memory("ns", "-", "topic-rebuilt", "alice is vegetarian", ["consolidated", "src:user_diet"])
    memory("ns", "-", "topic-rebuilt", "bob eats fish", ["consolidated", "src:bob-diet"], v=2)
    memory("ns2", "-", "topic-carol", "carol is vegan", ["consolidated", "src:user_diet"])
    # a summary of alice's fact whose row has merged away: only its history names her
    ch_client.insert(
        "loom.memory_versions",
        [[org, "ns", "a", "-", "-", "topic-gone", "fact", "alice is vegetarian", 1, consolidated]],
        column_names=_MEMORY_COLS,
    )
    # a summary tagged with alice's key in a namespace she holds no key in: not hers
    memory("ns3", "-", "topic-cross", "alice is vegetarian", ["consolidated", "src:user_diet"])
    vector = build_embedding_row(
        org=org,
        namespace="ns",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="topic-diets",
        content_excerpt="alice is vegetarian",
        tags=consolidated,
        embedding=[0.0] * 3072,
        memory_key_embedding=[0.0] * 3072,
        embedding_dim=3072,
        version=1,
    )
    ch_client.insert("loom.memory_embeddings", [list(vector.values())], column_names=list(vector))
    ch_client.insert(
        "loom.llm_response_cache",
        [
            ["m", "quoted-topic", org, "ns", "ask_synthesize", "vegetarian", ["topic-diets"]],
            ["m", "quoted-bob", org, "ns", "ask_synthesize", "fish", ["topic-bob"]],
            ["m", "quoted-carol", org, "ns2", "ask_synthesize", "vegan", ["topic-carol"]],
            ["m", "quoted-gone", org, "ns", "ask_synthesize", "vegetarian", ["topic-gone"]],
        ],
        column_names=["model", "key", "org", "namespace", "stage", "text", "window_keys"],
    )
    ch_client.insert(
        "loom.memory_ask_precedents",
        [
            [org, "ns", "a" * 64, "diets?", "vegetarian", ["topic-diets"], []],
            [org, "ns", "b" * 64, "bob?", "fish", [], ["topic-bob"]],
            [org, "ns2", "c" * 64, "carol?", "vegan", ["topic-carol"], []],
            [org, "ns", "d" * 64, "gone?", "vegetarian", [], ["topic-gone"]],
        ],
        column_names=["org", "namespace", "precedent_key", "question", "answer"]
        + ["cited_keys", "window_keys"],
    )


def _column(ch_client: Any, org: str, sql: str) -> list[str]:
    rows = ch_client.query(sql, parameters={"org": org}).result_rows
    return sorted(str(r[0]) for r in rows)


@pytest.mark.parametrize(("purge", "crash"), [(False, False), (True, False), (True, True)])
async def test_the_answers_that_quoted_a_consolidated_row_leave_with_its_source(
    ch_client, ch, monkeypatch: pytest.MonkeyPatch, purge: bool, crash: bool
) -> None:
    """`crash`: `memory.retire` stops after the summaries' delete, before the user's,
    and runs again, as a resumed saga does."""
    org = unique_org("erconsol")
    _seed(ch_client, org)
    caches = _COUNTED[1:]
    if crash:
        real = graph_steps.tombstone_scoped
        stopped: list[str] = []

        async def stop_once(table: str, **kw: Any) -> int:
            if kw["scope_where"] == UID and not stopped:
                stopped.append(table)
                raise RuntimeError("stopped")
            return await real(table, **kw)

        monkeypatch.setattr(graph_steps, "tombstone_scoped", stop_once)
    with erasure_mutations.purge_scope(purge), erasure_mutations.saga_scope(f"erasure-{org}"):
        before = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert {t: before[t] for t in _COUNTED} == {
            "loom.memory": 1,
            "loom.llm_response_cache": 2,
            "loom.memory_ask_precedents": 2,
        }
        for name, step in _STEPS[:-1]:
            await _run(name, step, org, ch)
        # counted while the user's keys still name the rows: a purge's census reads none
        mid = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert {t: mid[t] for t in caches} == dict.fromkeys(caches, 0)
        # an answer cached after the cache steps that quotes a summary they retired
        ch_client.insert(
            "loom.llm_response_cache",
            [["m", "quoted-late", org, "ns", "ask_synthesize", "vegetarian", ["topic-diets"]]],
            column_names=["model", "key", "org", "namespace", "stage", "text", "window_keys"],
        )
        # a row of alice's written live since memory.tombstone
        ch_client.insert(
            "loom.memory",
            [[org, "ns", "a", "alice", "-", "user_late", "fact", "alice is late", 1, []]],
            column_names=_MEMORY_COLS,
        )
        if crash:
            with pytest.raises(RuntimeError):
                await _run(*_STEPS[-1], org, ch)
        await _run(*_STEPS[-1], org, ch)
        after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
    assert after["loom.memory"] == 0

    memory = "SELECT memory_key FROM loom.memory FINAL WHERE org = {org:String}"
    live = _column(ch_client, org, f"{memory} AND tombstoned_at IS NULL")
    assert live == [
        "bob-diet",
        "topic-bob",
        "topic-carol",
        "topic-cross",
        "topic-rebuilt",
        "user_diet",
    ]
    # a tombstone keeps the rows; a purge leaves none of alice's or of the summaries'
    retired = ["topic-diets", "user_diet", "user_late"]
    kept = _column(ch_client, org, f"{memory} AND user_id IN ('alice', '-')")
    stays = ["topic-bob", "topic-carol", "topic-cross", "topic-rebuilt"]
    assert kept == (stays if purge else sorted([*stays, *retired]))
    if purge:  # every version, merged or not: the rebuilt summary keeps only its new one
        rebuilt = "SELECT value FROM loom.memory WHERE org = {org:String} AND memory_key = 'topic-rebuilt'"
        assert _column(ch_client, org, rebuilt) == ["bob eats fish"]
    vectors = "SELECT memory_key FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
    assert _column(ch_client, org, f"{vectors} AND tombstoned_at IS NULL") == []
    assert _column(ch_client, org, vectors) == ([] if purge else ["topic-diets"])
    cache = "SELECT key FROM loom.llm_response_cache WHERE org = {org:String}"
    assert _column(ch_client, org, cache) == ["quoted-bob", "quoted-carol"]
    prec = "SELECT question FROM loom.memory_ask_precedents WHERE org = {org:String}"
    assert _column(ch_client, org, prec) == ["bob?", "carol?"]
