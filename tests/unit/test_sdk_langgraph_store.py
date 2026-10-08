"""LoomStore: LangGraph's BaseStore contract over a loom Agent, without the
framework installed (structural twins) — keys, tags, filters, the four ops,
and the sync/async entry points."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from loom.integrations import langgraph as lg
from loom.memory import MemorySearchHit, MemorySearchResult


class _FakeMemory:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.sets: list[tuple[str, Any, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self.searches: list[dict[str, Any]] = []
        self.lists: list[dict[str, Any]] = []
        self.ranked: list[tuple[str, float]] = []

    async def set(self, key: str, value: Any, **kw: Any) -> dict[str, Any]:
        self.sets.append((key, value, kw))
        self.rows[key] = {
            "found": True,
            "value": value,
            "created_at": "2026-09-16T10:00:00Z",
            "updated_at": None,
        }
        return {"persisted": True}

    async def get(self, key: str, **kw: Any) -> dict[str, Any] | None:
        return self.rows.get(key)

    async def delete(self, key: str, **kw: Any) -> bool:
        self.deletes.append(key)
        return self.rows.pop(key, None) is not None

    async def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.searches.append({"query": query, **kw})
        return MemorySearchResult(
            [
                MemorySearchHit(
                    memory_key=k, memory_type="semantic", content_excerpt="", similarity=s
                )
                for k, s in self.ranked
            ]
        )

    async def list(self, **kw: Any) -> dict[str, Any]:
        self.lists.append(kw)
        prefix = kw.get("prefix") or ""
        keys = [k for k in sorted(self.rows) if k.startswith(prefix)]
        start, limit = kw.get("offset", 0), kw.get("limit", 100)
        page = keys[start : start + limit]
        return {
            "entries": [{"memory_key": k} for k in page],
            "truncated": start + limit < len(keys),
        }

    async def bulk_get(self, keys: list[str], **kw: Any) -> dict[str, Any]:
        return {
            "results": {k: self.rows[k] for k in keys if k in self.rows},
            "missing": [k for k in keys if k not in self.rows],
        }


class _FakeAgent:
    def __init__(self, memory: _FakeMemory) -> None:
        self.memory = memory
        self.closed_on: asyncio.AbstractEventLoop | None = None

    async def close(self) -> None:
        self.closed_on = asyncio.get_running_loop()


def _store(**kw: Any) -> tuple[lg.LoomStore, _FakeMemory]:
    mem = _FakeMemory()
    return lg.LoomStore(_FakeAgent(mem), **kw), mem  # type: ignore[arg-type]


# ---- mapping ----


def test_keys_and_tags_map_a_namespace_tuple_onto_a_prefix_and_one_tag_per_level() -> None:
    assert lg.memory_key_for(("users", "alice"), "prefs") == "lg/users/alice/prefs"
    assert lg.parse_memory_key("lg/users/alice/prefs") == (("users", "alice"), "prefs")
    assert lg.parse_memory_key("other/key") is None
    assert lg.parse_memory_key("lg/only") is None
    assert lg.tags_for(("users", "alice")) == ["lg:users", "lg:users/alice"]
    assert lg.prefix_tag(("users",)) == "lg:users" and lg.prefix_tag(()) is None
    with pytest.raises(ValueError):
        lg.memory_key_for(("a/b",), "k")
    with pytest.raises(ValueError):
        lg.memory_key_for(("a",), "")


def test_filter_operators_are_langgraphs() -> None:
    v = {"kind": "pref", "n": 3, "when": "2026"}
    assert lg.matches_filter(v, None) and lg.matches_filter(v, {})
    assert lg.matches_filter(v, {"kind": "pref"}) and not lg.matches_filter(v, {"kind": "fact"})
    assert lg.matches_filter(v, {"n": {"$gt": 2, "$lte": 3}}) and not lg.matches_filter(
        v, {"n": {"$gte": 4}}
    )
    assert lg.matches_filter(v, {"n": {"$ne": 4}}) and lg.matches_filter(v, {"n": {"$eq": 3}})
    assert not lg.matches_filter(v, {"missing": {"$gt": 0}})
    assert not lg.matches_filter(v, {"kind": {"$gt": 1}})  # str vs int: no match, no crash
    with pytest.raises(ValueError):
        lg.matches_filter(v, {"n": {"$in": [1]}})


# ---- the four ops ----


def test_put_get_delete_round_trip_with_tags_ttl_and_index_false() -> None:
    store, mem = _store(tags=["tenant:acme"])
    store.put(("users", "alice"), "prefs", {"seat": "aisle"}, ttl=5)
    store.put(("users", "alice"), "log", {"e": 1}, index=False)
    k, v, kw = mem.sets[0]
    assert k == "lg/users/alice/prefs" and v == {"seat": "aisle"}
    assert kw["tags"] == ["tenant:acme", "lg:users", "lg:users/alice"]
    assert kw["memory_type"] == "semantic" and kw["ttl_seconds"] == 300
    assert mem.sets[1][2]["memory_type"] == "episodic" and "ttl_seconds" not in mem.sets[1][2]

    item = store.get(("users", "alice"), "prefs")
    assert (
        item.value == {"seat": "aisle"}
        and item.key == "prefs"
        and item.namespace == ("users", "alice")
    )
    assert (
        item.created_at == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        and item.updated_at == item.created_at
    )
    assert store.get(("users", "alice"), "nope") is None

    store.delete(("users", "alice"), "prefs")
    assert mem.deletes == ["lg/users/alice/prefs"]
    assert store.get(("users", "alice"), "prefs") is None


def test_search_with_a_query_ranks_then_fetches_values_and_applies_prefix_filter_and_paging() -> (
    None
):
    store, mem = _store()
    store.put(("users", "alice"), "prefs", {"seat": "aisle", "kind": "pref"})
    store.put(("users", "alice"), "diet", {"food": "veg", "kind": "pref"})
    store.put(("users", "bob"), "prefs", {"seat": "window", "kind": "pref"})
    store.put(("teams", "x"), "rule", {"kind": "rule"})
    mem.ranked = [
        ("lg/users/alice/prefs", 0.9),
        ("lg/users/bob/prefs", 0.8),
        ("lg/teams/x/rule", 0.7),  # outside the prefix: dropped
        ("lg/users/alice/diet", 0.6),
        ("unrelated/key", 0.5),  # not this adapter's: dropped
    ]
    hits = store.search(("users",), query="seat", limit=2)
    assert [(h.namespace, h.key, h.score) for h in hits] == [
        (("users", "alice"), "prefs", 0.9),
        (("users", "bob"), "prefs", 0.8),
    ]
    assert hits[0].value == {"seat": "aisle", "kind": "pref"}
    s = mem.searches[0]
    assert s["tags"] == ["lg:users"] and s["top_k"] >= 2
    # paging: offset 2 → the third in-prefix hit
    page = store.search(("users",), query="seat", limit=5, offset=2)
    assert [h.key for h in page] == ["diet"]
    # a filter on the value
    only = store.search(("users",), query="seat", filter={"food": "veg"}, limit=5)
    assert [h.key for h in only] == ["diet"]


def test_search_without_a_query_lists_by_recency_under_the_prefix() -> None:
    store, mem = _store()
    store.put(("users", "alice"), "a", {"n": 1})
    store.put(("users", "alice"), "b", {"n": 2})
    store.put(("users", "bob"), "c", {"n": 3})
    items = store.search(("users", "alice"))
    assert [(i.key, i.score) for i in items] == [("a", None), ("b", None)]
    assert mem.lists[0]["prefix"] == "lg/users/alice/" and mem.lists[0]["tags"] == [
        "lg:users/alice"
    ]
    assert [i.key for i in store.search(("users", "alice"), filter={"n": {"$gte": 2}})] == ["b"]
    assert store.search(("nobody",)) == []


def test_list_namespaces_derives_tuples_with_prefix_suffix_depth_and_paging() -> None:
    store, mem = _store()
    store.put(("users", "alice", "prefs"), "k", {})
    store.put(("users", "bob"), "k", {})
    store.put(("teams", "x"), "k", {})
    store.put(("teams", "x"), "k2", {})
    mem.rows["other/thing"] = {"found": True, "value": {}}
    assert store.list_namespaces() == [
        ("teams", "x"),
        ("users", "alice", "prefs"),
        ("users", "bob"),
    ]
    assert store.list_namespaces(prefix=("users",)) == [
        ("users", "alice", "prefs"),
        ("users", "bob"),
    ]
    assert store.list_namespaces(prefix=("users", "*")) == [
        ("users", "alice", "prefs"),
        ("users", "bob"),
    ]
    assert store.list_namespaces(suffix=("prefs",)) == [("users", "alice", "prefs")]
    assert store.list_namespaces(max_depth=1) == [("teams",), ("users",)]
    assert store.list_namespaces(limit=1, offset=1) == [("users", "alice", "prefs")]
    assert mem.lists[-1]["fields"] == ["memory_key"]


# ---- entry points ----


def test_batch_executes_ops_in_order_and_aligns_results() -> None:
    store, mem = _store()
    results = store.batch(
        [
            lg.PutOp(("u",), "k", {"a": 1}),
            lg.GetOp(("u",), "k"),
            lg.SearchOp(("u",)),
            lg.ListNamespacesOp(),
            lg.PutOp(("u",), "k", None),
            lg.GetOp(("u",), "k"),
        ]
    )
    assert results[0] is None and results[1].value == {"a": 1}
    assert [i.key for i in results[2]] == ["k"] and results[3] == [("u",)]
    assert results[4] is None and results[5] is None
    with pytest.raises(TypeError):
        store.batch([object()])


def test_sync_calls_share_one_loop_that_close_shuts_with_the_agent() -> None:
    store, mem = _store()
    loops: list[asyncio.AbstractEventLoop] = []
    real_get = mem.get

    async def get(key: str, **kw: Any) -> dict[str, Any] | None:
        loops.append(asyncio.get_running_loop())
        return await real_get(key, **kw)

    mem.get = get  # type: ignore[method-assign]
    for i in range(3):
        store.put(("u",), f"k{i}", {"v": i})
        assert store.get(("u",), f"k{i}").value == {"v": i}
    assert len(loops) == 3 and len(set(map(id, loops))) == 1
    assert not loops[0].is_closed()

    store.close()
    assert store._agent.closed_on is loops[0] and loops[0].is_closed()
    store.close()  # idempotent


def test_async_entry_points_and_the_sync_one_refuses_inside_a_loop() -> None:
    store, _ = _store()

    async def go() -> None:
        await store.aput(("u",), "k", {"a": 1})
        assert (await store.aget(("u",), "k")).value == {"a": 1}
        assert [i.key for i in await store.asearch(("u",))] == ["k"]
        assert await store.alist_namespaces() == [("u",)]
        await store.adelete(("u",), "k")
        assert await store.aget(("u",), "k") is None
        with pytest.raises(RuntimeError, match="abatch"):
            store.get(("u",), "k")

    asyncio.run(go())


def test_scalar_loom_values_are_wrapped_and_dates_parse() -> None:
    store, mem = _store()
    mem.rows["lg/u/k"] = {
        "found": True,
        "value": "just text",
        "created_at": "2026-09-16T10:00:00+00:00",
        "updated_at": "2026-09-16T11:00:00Z",
    }
    item = store.get(("u",), "k")
    assert item.value == {"value": "just text"}
    assert item.updated_at.hour == 11 and item.updated_at.tzinfo is not None
    assert lg._dt(None).tzinfo is not None and lg._dt("garbage").tzinfo is not None


def test_framework_absent_here_uses_the_structural_twins() -> None:
    assert lg.HAS_LANGGRAPH is False
    assert lg.LoomStore.supports_ttl is True


def test_listing_pages_past_one_memory_list_page() -> None:
    store, mem = _store()
    for i in range(2500):
        mem.rows[f"lg/team/t{i:04d}/k"] = {
            "memory_key": f"lg/team/t{i:04d}/k",
            "value": {"i": i},
            "created_at": "2026-09-16T10:00:00Z",
        }
    assert len(store.list_namespaces(prefix=("team",), limit=5000)) == 2500
    assert [lst["offset"] for lst in mem.lists] == [0, 1000, 2000]
    mem.lists.clear()
    page = store.search(("team",), limit=2, offset=1500)
    assert [item.value["i"] for item in page] == [1500, 1501]
    assert [lst["offset"] for lst in mem.lists] == [0, 1000]  # stops once it has enough
