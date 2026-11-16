"""newest_wins retires the memory behind a closed triple and recency is arrival
order when the calendar cannot tell; plus the bench adapter's retry / dead-arm
guard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import projections as proj
from server.memory.app.ask import retrieval_legs
from server.memory.domain.graph_quality import IntervalRepair


class StubCH:
    def __init__(self) -> None:
        self.one: list[dict[str, Any] | None] = []
        self.all: list[list[dict[str, Any]]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.calls.append(("one", " ".join(sql.split()), params or {}))
        return self.one.pop(0) if self.one else None

    async def query_all(self, sql, params=None):
        self.calls.append(("all", " ".join(sql.split()), params or {}))
        return self.all.pop(0) if self.all else []


def _repair(subj="goaltender", rel="associated_with", old="ice hockey", new="pesäpallo"):
    return IntervalRepair(
        subject=subj,
        relation=rel,
        old_object=old,
        new_object=new,
        valid_to=datetime(2026, 9, 12, 8, 0),
        invalid_at=datetime(2026, 9, 12, 8, 0),
    )


def _mem(key, mid, pinned=False):
    return {
        "memory_id": mid,
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "s",
        "memory_key": key,
        "pinned": pinned,
    }


# --- newest_wins ----------------------------------------------------------------


def test_retirement_victims_retire_the_closed_carrier_and_name_the_survivor() -> None:
    links = [
        {
            "subject": "goaltender",
            "relation": "associated_with",
            "object": "ice hockey",
            "memory_key": "goaltender_sport",
        },
        {
            "subject": "goaltender",
            "relation": "associated_with",
            "object": "pesäpallo",
            "memory_key": "goaltender_pesapallo",
        },
        {
            "subject": "goaltender",
            "relation": "associated_with",
            "object": "ice hockey",
            "memory_key": "pinned_one",
        },
        {
            "subject": "goaltender",
            "relation": "associated_with",
            "object": "ice hockey",
            "memory_key": "both_values",
        },
        {
            "subject": "goaltender",
            "relation": "associated_with",
            "object": "pesäpallo",
            "memory_key": "both_values",
        },
    ]
    mems = [
        _mem("goaltender_sport", "m-old"),
        _mem("goaltender_pesapallo", "m-new"),
        _mem("pinned_one", "m-pin", pinned=True),
        _mem("both_values", "m-both"),
    ]
    victims = proj.retirement_victims([_repair()], links, mems, new_keys={"goaltender_pesapallo"})
    assert [(v["memory_key"], v["superseded_by"]) for v in victims] == [
        ("goaltender_sport", "m-new")
    ]
    v = victims[0]
    assert (v["memory_id"], v["agent"], v["user_id"], v["session_id"], v["namespace"]) == (
        "m-old",
        "a",
        "-",
        "s",
        "n",
    )


def test_retirement_victims_never_retire_a_definition() -> None:
    links = [{"subject": "g", "relation": "r", "object": "old", "memory_key": "definition/g"}]
    row = _mem("definition/g", "m1")  # a session copy: the path retires no definition
    definition = {**row, "memory_type": "definition"}
    repair = [_repair("g", "r", "old", "new")]
    assert proj.retirement_victims(repair, links, [definition], new_keys=set()) == []
    assert len(proj.retirement_victims(repair, links, [row], new_keys=set())) == 1


def test_retirement_victims_never_retire_the_batch_or_unknown_rows() -> None:
    links = [
        {"subject": "g", "relation": "r", "object": "old", "memory_key": "fresh"},
        {"subject": "g", "relation": "r", "object": "old", "memory_key": "gone"},
    ]
    assert (
        proj.retirement_victims(
            [_repair("g", "r", "old", "new")], links, [_mem("fresh", "m1")], new_keys={"fresh"}
        )
        == []
    )
    # a carrier with no live memory row is nothing to retire
    assert (
        proj.retirement_victims([_repair("g", "r", "old", "new")], links, [], new_keys=set()) == []
    )


@pytest.mark.asyncio
async def test_retire_behind_repairs_honours_the_policy_and_writes_through_supersede(
    monkeypatch,
) -> None:
    from server.memory import store as memory_repo

    inp = SimpleNamespace(org="o", namespace="n")
    tm = [{"subject": "g", "relation": "r", "object": "new", "memory_key": "k_new"}]
    # auto_reinforce: one policy read, nothing else
    ch = StubCH()
    ch.one.append({"default_conflict_policy": "auto_reinforce"})
    assert (
        await proj._retire_memories_behind_repairs(
            inp, [_repair("g", "r", "old", "new")], tm, ch=ch
        )
        == 0
    )
    assert [c[0] for c in ch.calls] == ["one"]

    # newest_wins: links → memories → supersede
    captured: list[Any] = []

    skips: list[Any] = []

    async def fake_supersede(org, victims, *, ch=None, skip=None, **_kw):
        captured.append((org, victims))
        skips.append(skip)
        return len(victims)

    monkeypatch.setattr(memory_repo, "supersede_duplicates", fake_supersede)
    ch = StubCH()
    ch.one.append({"default_conflict_policy": "newest_wins"})
    ch.all.append(
        [
            {"subject": "g", "relation": "r", "object": "old", "memory_key": "k_old"},
            {"subject": "g", "relation": "r", "object": "new", "memory_key": "k_new"},
        ]
    )
    ch.all.append([_mem("k_old", "m-old"), _mem("k_new", "m-new")])
    n = await proj._retire_memories_behind_repairs(
        inp, [_repair("g", "r", "old", "new")], tm, ch=ch
    )
    assert n == 1 and captured[0][0] == "o"
    assert (
        captured[0][1][0]["memory_key"] == "k_old" and captured[0][1][0]["superseded_by"] == "m-new"
    )
    links_sql = ch.calls[1][1]
    mem_sql = ch.calls[2][1]
    assert "loom.entity_triple_memories" in links_sql and "loom.memory FINAL" in mem_sql
    # read-your-writes on CH Cloud: the batch's own link rows must be visible
    assert "select_sequential_consistency = 1" in links_sql
    assert "select_sequential_consistency = 1" in mem_sql
    assert "status IN ('active', 'duplicate', 'conflicted')" in mem_sql
    assert "memory_type" in mem_sql

    # A definition the read returns stays live; the same row of another type retires.
    async def retired(memory_type: str) -> int:
        ch = StubCH()
        ch.one.append({"default_conflict_policy": "newest_wins"})
        ch.all.append([{"subject": "g", "relation": "r", "object": "old", "memory_key": "k_old"}])
        ch.all.append([{**_mem("k_old", "m-old"), "memory_type": memory_type}])
        return await proj._retire_memories_behind_repairs(
            inp, [_repair("g", "r", "old", "new")], tm, ch=ch
        )

    captured.clear()
    assert await retired("definition") == 0 and captured == []
    assert await retired("semantic") == 1

    # The row the read saw as a fact is refused once its current version is a definition.
    def current(memory_type: str) -> Any:
        row = {**_mem("k_old", "m-old"), "memory_type": memory_type}  # a session copy
        return SimpleNamespace(**row, status="active", lock_mode="")

    skip = skips[-1]
    assert skip(current("definition")) and not skip(current("semantic"))

    # no repairs → nothing, not even the policy read
    ch = StubCH()
    assert await proj._retire_memories_behind_repairs(inp, [], tm, ch=ch) == 0 and ch.calls == []


def test_newest_wins_is_a_declared_policy() -> None:
    import typing

    from server.identity.api.models import ConflictPolicyStr

    assert "newest_wins" in typing.get_args(ConflictPolicyStr)


# --- arrival-order recency --------------------------------------------------------


def _hit(key, sim, when):
    return SimpleNamespace(memory_key=key, similarity=sim, valid_at="", created_at=when)


def test_recency_is_arrival_order_when_the_set_spans_less_than_a_day() -> None:
    t0 = datetime(2026, 9, 12, 8, 0, 0)
    hits = [
        _hit("old", 0.80, (t0).isoformat()),
        _hit("new", 0.78, (t0 + timedelta(minutes=3)).isoformat()),
        _hit("mid", 0.79, (t0 + timedelta(minutes=1)).isoformat()),
        _hit("undated", 0.81, ""),
    ]
    out = retrieval_legs._recency_rerank(hits, 0.2)
    # newest gets +0.2, mid +0.1, oldest +0.0, undated +0 → 0.98, 0.89, 0.81, 0.80
    assert [h.memory_key for h in out] == ["new", "mid", "undated", "old"]


def test_recency_stays_calendar_based_when_the_set_spans_days() -> None:
    now = datetime.now(UTC)
    hits = [
        _hit("recent", 0.70, (now - timedelta(days=1)).isoformat()),
        _hit("stale", 0.72, (now - timedelta(days=200)).isoformat()),
    ]
    out = retrieval_legs._recency_rerank(hits, 0.2)
    # recent: 0.70 + 0.2/(1+1/14)=0.887 ; stale: 0.72 + 0.2/(1+200/14)=0.733
    assert [h.memory_key for h in out] == ["recent", "stale"]
    assert retrieval_legs._recency_rerank([], 0.2) == []


def test_wire_hit_carries_created_at() -> None:
    from server.memory.api.models_search import _MemorySearchHit

    h = _MemorySearchHit(
        memory_key="k", memory_type="semantic", content_excerpt="", distance=0.1, similarity=0.9
    )
    assert h.created_at == ""


# --- bench adapter ------------------------------------------------------------------


def test_bench_retry_delay_and_dead_arm_guard(monkeypatch, tmp_path) -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "mab", Path(__file__).resolve().parents[2] / "bench" / "memoryagentbench.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert mod.retry_delay("7", 0) == 7.0 and mod.retry_delay("900", 0) == 60.0
    assert mod.retry_delay(None, 0) == 0.5 and mod.retry_delay("soon", 3) == 4.0

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.headers = {"retry-after": "0"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self):
            self.codes = [429, 429, 200]
            self.n = 0

        async def post(self, url, json=None, headers=None, **kw):
            self.n += 1
            return _Resp(self.codes.pop(0))

    c = _Client()
    assert asyncio.run(mod._post(c, "http://x/v1/knowledge.add", {})) == {"ok": True} and c.n == 3

    data = tmp_path / "d.jsonl"
    data.write_text("")
    monkeypatch.setattr(
        "sys.argv", ["mab", "--data", str(data), "--memorize", "knowledge", "--answer", "ask"]
    )
    assert mod.main() == 2


@pytest.mark.asyncio
async def test_retire_behind_repairs_reads_only_rows_in_the_writers_scope() -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.domain.read_scope import (
        WHOLE_NAMESPACE,
        ReadFilters,
        bind_request_read_filters,
    )

    auth = AuthInfo(subject="sa:bot", org="o", namespaces=("n",), roles=(), is_service_account=True)
    set_current_auth(auth)
    bind_request_read_filters(auth, ReadFilters(scope="own_user", user_id="u1", session_id=None))
    try:
        ch = StubCH()
        ch.one.append({"default_conflict_policy": "newest_wins"})
        ch.all.append([{"subject": "g", "relation": "r", "object": "old", "memory_key": "k"}])
        inp = SimpleNamespace(org="o", namespace="n")
        await proj._retire_memories_behind_repairs(
            inp, [_repair("g", "r", "old", "new")], [], ch=ch
        )
    finally:
        set_current_auth(None)
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    _, mem_sql, params = ch.calls[2]
    # Another user's row of the same fact is never read, so it never retires.
    assert "user_id = {scope_uid:String}" in mem_sql and params["scope_uid"] == "u1"
