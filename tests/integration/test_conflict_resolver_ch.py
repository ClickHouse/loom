"""The conflict resolver end to end against ClickHouse, with a scripted LLM.

A newest_wins namespace holds four pairs, each in its own direction of the vector
space so no cluster reaches another pair: an update, a compatible pair already
flagged `conflicted` by cosine, a pair the model is unsure about, and a pair whose
older row is pinned. One pass retires the update's older row (with its record, its
audit row and the graph following), clears the false flag and leaves the other
two alone; a second pass changes nothing; Undo brings the retired row and its fact
back."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

import pytest

from server.memory.domain.conflict_resolution import PROMPT_VERSION
from server.memory.store import lifecycle
from tests.integration._resources import unique_org

NS = "resolver"
USER = "u1"
DIM = 3072


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


def _vec(*parts: tuple[int, float]) -> list[float]:
    v = [0.0] * DIM
    for i, x in parts:
        v[i] = x
    return v


# The scripted judge: per NEW key, (verdict, confidence) for every peer it is shown.
_SCRIPT = {
    "office_location": ("update", 0.98),
    "lead_name": ("unrelated", 0.97),
    "standup_lead": ("unrelated", 0.97),
    "budget_next": ("update", 0.80),
    "deploy_day_now": ("update", 0.99),
}


class _ScriptedLLM:
    provider, model = "fake", "scripted"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.orgs: list[str] = []

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        from types import SimpleNamespace

        key = re.search(r"NEW note \(key ([^,)]+)", user).group(1)  # type: ignore[union-attr]
        self.calls.append(key)
        verdict, confidence = _SCRIPT.get(key, ("related", 0.5))
        peers = re.findall(r"^\[(p\d+)\]", user, re.M)
        entries = [
            {
                "peer_id": p,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": verdict,
                "confidence": confidence,
            }
            for p in peers
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))


async def _set(org: str, session: str, key: str, value: str, user: str = USER, **kw: Any) -> str:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=user,
            session_id=session,
            memory_key=key,
            value=value,
            **kw,
        )
    )
    return str(out.memory_id)


async def _place(
    ch: Any, org: str, session: str, key: str, vec: list[float], owed: bool, user: str = USER
) -> None:
    """Give the row's vector a fixed direction, and the resolver's mark when `owed`."""
    from server.memory.store.conflict_resolver import OWED_MARK
    from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows

    rows = await live_embedding_rows_for_key(
        ch, org=org, namespace=NS, agent="bot", user_id=user, session_id=session, memory_key=key
    )
    assert rows, key
    await reemit_rows(
        rows, {"embedding": vec, "conflict_checked_at": OWED_MARK if owed else None}, ch=ch
    )


async def _rows(ch: Any, org: str) -> dict[str, dict[str, Any]]:
    rows = await ch.query_all(
        "SELECT memory_key, toString(memory_id) AS id, status, superseded_by, conflict_with, "
        "pinned, version FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String}"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": NS},
    )
    return {str(r["memory_key"]): dict(r) for r in rows}


async def _marks(ch: Any, org: str) -> dict[str, int | None]:
    rows = await ch.query_all(
        "SELECT memory_key, toUnixTimestamp(conflict_checked_at) AS mark, status "
        "FROM loom.memory_embeddings FINAL WHERE org = {org:String} AND namespace = {ns:String}"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": NS},
    )
    return {str(r["memory_key"]): r["mark"] for r in rows}


async def _paris_open(ch: Any, org: str, want: bool) -> bool:
    # Closes are mutations: poll up to ten seconds for `want`.
    n = 0
    for _ in range(50):
        rows = await ch.query_all(
            "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
            " AND object = 'paris' AND valid_to IS NULL"
            " SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1",
            params={"org": org},
        )
        n = int(rows[0]["n"])
        if (n > 0) == want:
            break
        await asyncio.sleep(0.2)
    return n > 0


class _OnlyOrgs:
    """The queue reads, limited to this test's orgs (the server holds other tests')."""

    def __init__(self, ch: Any, orgs: list[str]) -> None:
        self.ch, self.orgs = ch, orgs

    async def query_all(self, sql: str, params: Any = None) -> Any:
        sql = sql.replace("WHERE ", "WHERE org IN {only_orgs:Array(String)} AND ")
        return await self.ch.query_all(sql, params={**(params or {}), "only_orgs": self.orgs})


@pytest.fixture
def wired(ch, monkeypatch: pytest.MonkeyPatch) -> Any:
    import server.llm.client as llm_client
    from server.memory.app import conflicts
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers import conflict_resolver as worker
    from server.settings import settings

    org = unique_org("resolver")
    llm = _ScriptedLLM()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: llm)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)

    async def newest_wins(org: str, namespace: str, ch: Any = None) -> str:
        return "newest_wins"

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", newest_wins)
    # Every pass reads the recheck: a test's flags are judged in the pass it runs.
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_recheck_idle_seconds", 0.0)
    monkeypatch.setattr(worker, "_state", worker._State())  # another test's pass may have set it
    # A test that needs a second org appends it to `orgs`.
    only = _OnlyOrgs(ch, [org])
    for name in ("queued_namespaces", "owed_rows", "unjudged_conflicted_rows"):
        read = getattr(queue, name)
        monkeypatch.setattr(queue, name, lambda read=read, **kw: read(**kw, ch=only))
    llm.orgs = only.orgs
    return org, llm


async def _drop_vectors(ch: Any, org: str, namespace: str, key_prefix: str) -> None:
    """Drop vector rows a test wrote with no memory row behind them. They are orphans
    to the orphan reconcile, whose pass is not scoped to one test and heals 500 keys in
    org order: left in place, they take the places of a later test's key."""
    await ch.execute(
        "DELETE FROM loom.memory_embeddings WHERE org = {o:String} AND namespace = {n:String}"
        " AND startsWith(memory_key, {p:String})",
        params={"o": org, "n": namespace, "p": key_prefix},
    )


def _policy(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from server.memory.app import conflicts

    async def policy(org: str, namespace: str, ch: Any = None) -> str:
        return name

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", policy)


async def _later() -> None:
    """Rows are stated to the second; the next write is a second later than the last."""
    await asyncio.sleep(1.1)


async def _seed(ch: Any, org: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    # Seeded without the write-time check: the test places every vector itself.
    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        office = ("office", "located_in", "paris")
        await _set(org, "s1", "office_city", "The office is in Paris", relations=[office])
        await _set(org, "s1", "lead_name", "The team lead is Ana")
        await _set(org, "s1", "budget_now", "The budget is ten thousand")
        await _set(org, "s1", "deploy_day", "Deploys go out on Fridays", pinned=True)
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await _set(org, "s2", "standup_lead", "Ana runs the Monday standup")
        await _set(org, "s2", "budget_next", "Next year's budget is twelve thousand")
        await _set(org, "s2", "deploy_day_now", "Deploys go out on Thursdays")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((0, 1.0)), owed=False)
    await _place(ch, org, "s2", "office_location", _vec((0, 0.8), (1, 0.6)), owed=True)
    await _place(ch, org, "s1", "lead_name", _vec((2, 1.0)), owed=False)
    await _place(ch, org, "s2", "standup_lead", _vec((2, 0.9), (3, 0.436)), owed=False)
    await _place(ch, org, "s1", "budget_now", _vec((4, 1.0)), owed=False)
    await _place(ch, org, "s2", "budget_next", _vec((4, 0.75), (5, 0.66)), owed=True)
    await _place(ch, org, "s1", "deploy_day", _vec((6, 1.0)), owed=False)
    await _place(ch, org, "s2", "deploy_day_now", _vec((6, 0.8), (7, 0.6)), owed=True)
    # The compatible pair, flagged by cosine alone before this build judged it.
    rows = await _rows(ch, org)
    landed = await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["standup_lead"]["id"],
        peer_ids=[rows["lead_name"]["id"]],
        namespace=NS,
    )
    assert landed == "conflict"


async def _audit(ch: Any, org: str, event: str) -> list[dict[str, Any]]:
    rows = await ch.query_all(
        "SELECT subject FROM loom.audit_log WHERE org = {org:String} AND event_type = {e:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "e": event},
    )
    return [json.loads(r["subject"]) for r in rows]


async def test_one_pass_retires_clears_and_leaves_the_rest_then_changes_nothing(
    ch, wired, monkeypatch
) -> None:
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import after_write
    from server.memory.app.restore import handle_memory_restore
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _seed(ch, org, monkeypatch)
    before = await _rows(ch, org)
    assert before["lead_name"]["status"] == "conflicted" and await _paris_open(ch, org, True)

    out = await run_pass()
    await after_write.drain()
    rows = await _rows(ch, org)
    # The update: the older row retires behind the newer, its fact closes.
    assert rows["office_city"]["status"] == "superseded"
    assert rows["office_city"]["superseded_by"] == rows["office_location"]["id"]
    assert rows["office_location"]["status"] == "active"
    assert not await _paris_open(ch, org, False)
    # The false flag: cleared on both sides.
    for k in ("lead_name", "standup_lead"):
        assert (rows[k]["status"], rows[k]["conflict_with"]) == ("active", []), k
    # Unsure, and pinned: untouched.
    for k in ("budget_now", "budget_next", "deploy_day", "deploy_day_now"):
        assert rows[k]["status"] == "active" and rows[k]["version"] == before[k]["version"], k
    assert out["retired"] == 1 and out["cleared"] == 1 and out["skipped_pinned"] == 0
    assert "deploy_day_now" not in llm.calls, "a pinned row is no peer: no call is paid for it"
    assert out["skipped_no_action"] >= 1 and out["errored"] == 0
    # Every judged row is stamped at its write; search reads the cleared rows as active.
    marks = await _marks(ch, org)
    for k in ("office_location", "budget_next", "deploy_day_now", "lead_name", "standup_lead"):
        assert marks[k] == 2, k
    status = await ch.query_all(
        "SELECT memory_key, status FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
        " AND memory_key IN ('lead_name', 'standup_lead', 'office_city')"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["memory_key"]: r["status"] for r in status} == {
        "lead_name": "active",
        "standup_lead": "active",
        "office_city": "superseded",
    }
    # The record of the retirement, and the hash-only audit row of each judgement that acted.
    (record,) = await _audit(ch, org, "memory.auto_supersede")
    assert record["reason"] == "conflict_resolver"
    assert [(e["memory_id"], e["label"]) for e in record["retired"]] == [
        (rows["office_city"]["id"], "update")
    ]
    acted = await _audit(ch, org, "memory.conflict_resolver")
    assert sorted(p["action"] for a in acted for p in a["pairs"]) == ["clear", "retire"]
    assert "Paris" not in json.dumps(acted)

    # Again: nothing is owed, nothing changes, the LLM is not called.
    calls = len(llm.calls)
    again = await run_pass()
    assert len(llm.calls) == calls and again["judged"] == 0
    after = await _rows(ch, org)
    assert {k: (r["status"], r["version"]) for k, r in after.items()} == {
        k: (r["status"], r["version"]) for k, r in rows.items()
    }

    # Undo brings the retired row back, and the fact it states.
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=rows["office_location"]["id"],
        )
    )
    assert undone.restored
    assert (await _rows(ch, org))["office_city"]["status"] == "active"
    assert await _paris_open(ch, org, True)
    # The Undo holds: the next pass does not judge the pair again, or retire it.
    calls = len(llm.calls)
    after_undo = await run_pass()
    assert len(llm.calls) == calls and after_undo["retired"] == 0, after_undo
    assert (await _rows(ch, org))["office_city"]["status"] == "active"
    marks = await _marks(ch, org)
    assert 1 not in (marks["office_city"], marks["office_location"]), "neither row is owed"


async def test_a_write_with_a_peer_past_the_floor_is_owed_a_judgement(ch, wired) -> None:
    """The write path marks a row whose peer sits between the 0.60 floor and the
    threshold (0.65 here); one whose nearest peer is at 0.55, or that has none that
    near, is not marked."""
    from server.memory.app import after_write

    org, _llm = wired
    await _set(org, "s1", "pet", "The user has a dog named Rex")
    await after_write.drain()
    await _place(ch, org, "s1", "pet", _vec((10, 1.0)), owed=False)

    from server.embedding import get_embedder
    from server.memory.app import set_commit

    real = get_embedder()

    class Placed:
        """The real embedder's name and width, with vectors placed by the test."""

        dim, model_name = real.dim, real.model_name

        async def embed(self, text: str) -> list[float]:
            if "cat" in text:
                return _vec((10, 0.65), (11, 0.76))
            if "parrot" in text:
                return _vec((10, 0.55), (13, 0.835))
            return _vec((12, 1.0))

    with pytest.MonkeyPatch.context() as m:
        m.setattr(set_commit, "get_embedder", lambda: Placed())
        await _set(org, "s2", "pet_new", "The user also has a cat named Tom")
        await _set(org, "s2", "bird", "The user keeps a parrot named Kiwi")
        await _set(org, "s2", "car", "The user drives a blue car")
    marks = await _marks(ch, org)
    assert marks["pet_new"] == 1 and marks["bird"] is None and marks["car"] is None


class _SlowLLM:
    """Answers `update` for the peer keyed `office_city` and `unrelated` for the rest,
    after a pause, counting the calls in flight at once."""

    provider, model = "fake", "slow"

    def __init__(self) -> None:
        self.in_flight = self.most_in_flight = 0

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        from types import SimpleNamespace

        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(0.3)
        self.in_flight -= 1
        entries = [
            {
                "peer_id": p,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": "update" if key == "office_city" else "unrelated",
                "confidence": 0.99,
            }
            for p, key in re.findall(r"^\[(p\d+)\] \(key ([^,]+),", user, re.M)
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))


async def test_two_rows_judged_at_once_retire_their_shared_peer_once(
    ch, wired, monkeypatch
) -> None:
    """Two owed rows whose clusters both hold the same older row are judged at the
    same time and both call it stale: it is retired once, behind one of them."""
    import server.llm.client as llm_client
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    llm = _SlowLLM()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: llm)

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await _later()
        await _set(org, "s2", "office_a", "The office moved to Rome")
        await _set(org, "s3", "office_b", "The office is in Rome now")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((20, 1.0)), owed=False)
    await _place(ch, org, "s2", "office_a", _vec((20, 0.8), (21, 0.6)), owed=True)
    await _place(ch, org, "s3", "office_b", _vec((20, 0.8), (22, 0.6)), owed=True)

    out = await run_pass()
    rows = await _rows(ch, org)
    assert llm.most_in_flight == 2
    assert rows["office_city"]["status"] == "superseded"
    assert rows["office_city"]["superseded_by"] in (rows["office_a"]["id"], rows["office_b"]["id"])
    assert rows["office_a"]["status"] == rows["office_b"]["status"] == "active"
    assert out["retired"] == 1 and out["skipped_changed"] == 1 and out["errored"] == 0
    # One record of the retirement, and one versioned write of it.
    (record,) = await _audit(ch, org, "memory.auto_supersede")
    assert [e["memory_id"] for e in record["retired"]] == [rows["office_city"]["id"]]
    # What decided it rides the entry, for the audit record.
    (entry,) = record["retired"]
    assert entry["judge_verdict"] == "update" and float(entry["judge_confidence"]) >= 0.98
    assert entry["judge_model"] and entry["judge_prompt_version"] == PROMPT_VERSION
    # Distinct versions: the ReplacingMergeTree keeps a repeat of one until a merge.
    versions = await ch.query_all(
        "SELECT uniqExact(version) AS n FROM loom.memory_versions WHERE org = {org:String}"
        " AND memory_key = 'office_city' AND status = 'superseded'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert int(versions[0]["n"]) == 1
    marks = await _marks(ch, org)
    assert marks["office_a"] == marks["office_b"] == 2


async def test_a_namespace_with_one_owed_row_is_served_beside_a_bulk_import(
    ch, wired, monkeypatch
) -> None:
    """Namespace `bulk` holds 500 owed rows, all older; the one owed row of another
    namespace is still judged in the first pass."""
    from datetime import timedelta

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.store.conflict_resolver import OWED_MARK
    from server.memory.store.embeddings import (
        _copy_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
    )
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((30, 1.0)), owed=False)
    await _place(ch, org, "s2", "office_location", _vec((30, 0.8), (31, 0.6)), owed=True)
    (row,) = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s2",
        memory_key="office_location",
    )
    # The import: vector rows only (no memory row behind them), each owed, all older.
    older = row["created_at"] - timedelta(hours=1)
    bulk = [
        _copy_row(
            row,
            {
                "namespace": "bulk",
                "memory_key": f"k{i}",
                "created_at": older + timedelta(seconds=i),
                "conflict_checked_at": OWED_MARK,
            },
        )
        for i in range(500)
    ]
    await insert_embedding_rows(bulk, ch=ch)
    try:
        await run_pass()
        assert "office_location" in llm.calls
        assert (await _marks(ch, org))["office_location"] == 2
    finally:
        await _drop_vectors(ch, org, "bulk", "k")


async def _directory(ch: Any, org: str, *, active: bool, at: Any) -> None:
    """The namespace's directory row, as namespace.register / namespace.delete write it."""
    row = {"org": org, "namespace": NS, "created_at": at, "updated_at": at, "active": active}
    await ch.insert("loom.namespaces", [row])


async def test_a_deleted_namespace_is_not_judged_until_it_is_registered_again(
    ch, wired, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((40, 1.0)), owed=False)
    await _place(ch, org, "s2", "office_location", _vec((40, 0.8), (41, 0.6)), owed=True)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await _directory(ch, org, active=True, at=now - timedelta(seconds=10))
    await _directory(ch, org, active=False, at=now - timedelta(seconds=5))

    out = await run_pass()
    assert llm.calls == [] and out["judged"] == 0
    assert (await _marks(ch, org))["office_location"] == 1

    await _directory(ch, org, active=True, at=now)
    await run_pass()
    assert llm.calls == ["office_location"]
    assert (await _marks(ch, org))["office_location"] == 2


async def _pair(ch: Any, org: str, old: str, new: str, axis: int, *, gap: bool = True) -> None:
    """An older row and, a second later (or at once), a newer owed one close to it."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with pytest.MonkeyPatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", old, "The office is in Paris")
        if gap:
            await _later()
        await _set(org, "s2", new, "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", old, _vec((axis, 1.0)), owed=False)
    await _place(ch, org, "s2", new, _vec((axis, 0.8), (axis + 1, 0.6)), owed=True)


async def test_a_row_the_provider_refuses_does_not_stop_another_orgs_update(
    ch, wired, monkeypatch
) -> None:
    """Org X's row always gets a 400; org Y's update is judged in the same pass, and
    X's row is stamped so it does not come back."""
    import httpx

    from server.memory.workers.conflict_resolver import run_pass

    org_y, llm = wired
    org_x = unique_org("resolver-x")
    llm.orgs.append(org_x)
    await _pair(ch, org_x, "x_city", "x_location", 50)
    await _pair(ch, org_y, "office_city", "office_location", 50)
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        if "NEW note (key x_location," in user:
            req = httpx.Request("POST", "https://llm.example/v1")
            raise httpx.HTTPStatusError(
                "400 Bad Request", request=req, response=httpx.Response(400, request=req)
            )
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    out = await run_pass()
    assert (await _rows(ch, org_y))["office_city"]["status"] == "superseded"
    assert out["errored"] == 1 and out["retired"] == 1
    x = await ch.query_all(
        "SELECT toUnixTimestamp(conflict_checked_at) AS mark FROM loom.memory_embeddings FINAL"
        " WHERE org = {org:String} AND memory_key = 'x_location'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org_x},
    )
    assert [r["mark"] for r in x] == [2], "refused: stamped, it holds no place next pass"


@pytest.mark.parametrize(
    ("status", "reason", "left_out"),
    [
        ("in_progress", "", True),
        ("failed", "step erase_memory failed closed", True),  # the recovery worker retries it
        ("failed", "max_attempts (5) exceeded", False),  # abandoned: never runs again
        ("completed", "", False),
    ],
)
async def test_an_org_under_erasure_is_left_out_of_the_queue(
    ch, wired, status: str, reason: str, left_out: bool
) -> None:
    import uuid
    from datetime import UTC, datetime

    from server.memory.store import conflict_resolver as queue

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 52)
    assert [r["memory_key"] for r in await queue.owed_rows(limit=10)] == ["office_location"]
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await ch.insert(
        "loom.erasure_requests",
        [
            {
                "id": str(uuid.uuid4()),
                "org": org,
                "scope_type": "org",
                "scope_value": org,
                "requester": "test",
                "status": status,
                "failure_reason": reason,
                "requested_at": now,
                "last_checkpoint_at": now,
                "version": 1,
            }
        ],
    )
    owed = [r["memory_key"] for r in await queue.owed_rows(limit=10)]
    assert owed == ([] if left_out else ["office_location"])
    assert (await queue.queued_namespaces() == []) is left_out


@pytest.mark.parametrize("path", ["memory.set", "memory.bulk_set"])
async def test_an_empty_user_is_the_anonymous_user_and_owes_no_other_session(
    ch, wired, monkeypatch, path
) -> None:
    """A write sent with user_id "" is stored as sent, and the anonymous user's rules
    hold for it: the same key from another session is owed no judgement."""
    from server.embedding import get_embedder
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set, set_commit
    from server.memory.app.bulk_set import handle_memory_bulk_set

    org, _ = wired
    _policy(monkeypatch, "auto_reinforce")
    real = get_embedder()

    class Apart:
        """Every text in its own direction: no cosine peer at all."""

        dim, model_name = real.dim, real.model_name
        n = 200

        async def embed(self, text: str) -> list[float]:
            Apart.n += 1
            return _vec((Apart.n, 1.0))

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [await self.embed(t) for t in texts]

    monkeypatch.setattr(set_commit, "get_embedder", lambda: Apart())
    monkeypatch.setattr(bulk_set, "get_embedder", lambda: Apart(), raising=False)

    async def write(session: str, value: str) -> None:
        if path == "memory.set":
            await _set(org, session, "office", value, user="")
            return
        entry = {"memory_key": "office", "value": value, "memory_type": "semantic"}
        await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org, namespace=NS, agent="bot", user_id="", session_id=session, entries=[entry]
            )
        )

    await write("s1", "office: Paris")
    await after_write.drain()
    await write("s2", "The office moved to Rome last week")
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT user_id, session_id, toUnixTimestamp(conflict_checked_at) AS mark"
        " FROM loom.memory_embeddings FINAL WHERE org = {org:String} AND memory_key = 'office'"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["user_id"] for r in rows} == {""}
    assert {r["session_id"]: r["mark"] for r in rows} == {"s1": None, "s2": None}


@pytest.mark.parametrize(("first", "second"), [("", ""), ("-", "-"), ("", "-"), ("-", "")])
async def test_a_named_users_rewrite_in_its_own_session_is_owed_nothing(
    ch, wired, monkeypatch, first, second
) -> None:
    """A named user rewriting a key with no session (sent empty or '-', either spelling
    each time) holds the key in no other session, so the rewrite owes no judgement."""
    from server.memory.app import after_write

    org, _ = wired
    _policy(monkeypatch, "auto_reinforce")
    await _set(org, first, "office", "office: Paris", user="u1")
    await after_write.drain()
    await _set(org, second, "office", "office: Rome", user="u1")
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT toUnixTimestamp(conflict_checked_at) AS mark"
        " FROM loom.memory_embeddings FINAL WHERE org = {org:String} AND memory_key = 'office'"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows and all(r["mark"] is None for r in rows), rows


async def test_the_anonymous_users_two_spellings_are_one_subject(ch, wired, monkeypatch) -> None:
    """One agent's rows under user '-' and under a user_id sent empty are one subject:
    each is the other's peer in the cluster."""
    from server.memory.store import conflict_resolver as queue

    org, _ = wired
    await _set(org, "s1", "office_city", "The office is in Paris", user="-")
    await _set(org, "s1", "office_location", "The office moved to Rome", user="")
    await _place(ch, org, "s1", "office_city", _vec((90, 1.0)), owed=False, user="-")
    await _place(ch, org, "s1", "office_location", _vec((90, 0.8), (91, 0.6)), owed=True, user="")
    (row,) = [r for r in await queue.owed_rows(limit=50) if r["org"] == org]
    cluster = await queue.cluster_peers(row, floor=0.7, limit=8, ch=ch)
    assert [(p["memory_key"], p["user_id"]) for p in cluster.peers] == [("office_city", "-")]


async def test_a_spelling_twin_of_the_row_is_its_peer(ch, wired, monkeypatch) -> None:
    """The row ('-' user, '-' session) and its twin under the other spelling of no user
    ("" user, '-' session) share agent, session and key, yet are two rows: the twin is
    in the row's cluster, so the pair is judged."""
    from server.memory.store import conflict_resolver as queue

    org, _ = wired
    await _set(org, "-", "office", "The office is in Paris", user="")
    await _set(org, "-", "office", "The office moved to Rome", user="-")
    await _place(ch, org, "-", "office", _vec((92, 1.0)), owed=False, user="")
    await _place(ch, org, "-", "office", _vec((92, 0.8), (93, 0.6)), owed=True, user="-")
    (row,) = [r for r in await queue.owed_rows(limit=50) if r["org"] == org]
    cluster = await queue.cluster_peers(row, floor=0.6, limit=8, ch=ch)
    assert [(p["memory_key"], p["user_id"]) for p in cluster.peers] == [("office", "")]


async def test_other_anonymous_sessions_take_no_place_from_a_same_session_peer(
    ch, wired, monkeypatch
) -> None:
    """Forty other sessions of the anonymous user hold the row's key, nearer than its
    same-session peer and more than the nearest read returns. They have no flag with the
    row, so the reads leave them out, and the same-session pair is judged."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        for i in range(40):
            await _set(org, f"other{i}", "status", f"build {i} is green", user="-")
        await _set(org, "s1", "office_city", "The office is in Paris", user="-")
        await _set(org, "s1", "status", "The office moved to Rome", user="-")
        await after_write.drain()
    for i in range(40):
        vec = _vec((300, 1.0), (301, 0.01 * i))
        await _place(ch, org, f"other{i}", "status", vec, owed=False, user="-")
    await _place(ch, org, "s1", "office_city", _vec((300, 0.9), (302, 0.436)), owed=False, user="-")
    await _place(ch, org, "s1", "status", _vec((300, 1.0)), owed=True, user="-")
    (row,) = [r for r in await queue.owed_rows(limit=50) if r["org"] == org]
    cluster = await queue.cluster_peers(row, floor=0.6, limit=8, ch=ch)
    assert [(p["memory_key"], p["session_id"]) for p in cluster.peers] == [("office_city", "s1")]
    await run_pass()
    assert llm.calls == ["status"]


async def test_a_point_write_of_a_key_another_session_holds_is_owed(ch, wired, monkeypatch) -> None:
    """memory.set and memory.update_by_id from s2 of a key s1 holds live are owed a
    judgement under auto_reinforce, though no peer sits near them."""
    from server.embedding import get_embedder
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import after_write, set_commit, update
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    _policy(monkeypatch, "auto_reinforce")
    real = get_embedder()

    class Apart:
        """Every text in its own direction: no cosine peer at all."""

        dim, model_name = real.dim, real.model_name
        n = 60

        async def embed(self, text: str) -> list[float]:
            Apart.n += 1
            return _vec((Apart.n, 1.0))

    monkeypatch.setattr(set_commit, "get_embedder", lambda: Apart())
    monkeypatch.setattr(update, "get_embedder", lambda: Apart(), raising=False)
    await _set(org, "s1", "office", "office: Paris")
    await _set(org, "s2", "car", "The user drives a blue car")
    await after_write.drain()
    marks = await _marks(ch, org)
    assert marks["office"] is None and marks["car"] is None
    second = await _set(org, "s2", "office", "The office moved to Rome last week")
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT session_id, toUnixTimestamp(conflict_checked_at) AS mark"
        " FROM loom.memory_embeddings FINAL WHERE org = {org:String} AND memory_key = 'office'"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["session_id"]: r["mark"] for r in rows} == {"s1": None, "s2": 1}
    # A revision of s2's row is owed again: s1 still holds the key.
    await update.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org=org, memory_id=second, value="The office is in Rome")
    )
    rows = await ch.query_all(
        "SELECT session_id, toUnixTimestamp(conflict_checked_at) AS mark"
        " FROM loom.memory_embeddings FINAL WHERE org = {org:String} AND memory_key = 'office'"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["session_id"]: r["mark"] for r in rows} == {"s1": None, "s2": 1}
    # The pass judges s2's row against s1's, though they sit far below the floor.
    prompts: list[str] = []
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        prompts.append(user)
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    await run_pass()
    assert llm.calls == ["office"] and "office: Paris" in prompts[0]


async def test_rows_wait_owed_without_the_key_and_are_judged_once_it_is_set(
    ch, wired, monkeypatch, caplog
) -> None:
    import server.llm.client as llm_client
    from server.llm.fake import FakeLLMClient
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 54)
    # No key: the client is the fake stand-in.
    monkeypatch.setattr(
        llm_client, "get_llm_client_without_fallback", lambda m, **kw: FakeLLMClient(model=m)
    )
    with caplog.at_level("WARNING"):
        out = await run_pass()
        again = await run_pass()
    assert out["judged"] == again["judged"] == 0 and out["errored"] == 0
    assert sum("unavailable" in r.message for r in caplog.records) == 1, "once, not per pass"
    assert (await _marks(ch, org))["office_location"] == 1, "still owed"
    # The key is set: the same row is judged.
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: llm)
    await run_pass()
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    assert (await _marks(ch, org))["office_location"] == 2


async def test_rows_written_in_one_second_retire_by_the_order_their_values_were_asserted(
    ch, wired
) -> None:
    """No gap between the writes, and the older row's tags are rewritten after the newer
    one was written (a new version and updated_at, the same hash): it still retires."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 56, gap=False)
    before = await _rows(ch, org)
    await _later()
    await handle_memory_update_by_id(
        MemoryUpdateByIdInput(org=org, memory_id=before["office_city"]["id"], tags=["office"])
    )
    # The revision re-embeds the row: put its vector back where the test placed it.
    await _place(ch, org, "s1", "office_city", _vec((56, 1.0)), owed=False)
    out = await run_pass()
    after = await _rows(ch, org)
    assert after["office_city"]["version"] > before["office_city"]["version"]
    assert after["office_city"]["status"] == "superseded", out
    assert after["office_location"]["status"] == "active"


@pytest.mark.parametrize("relabelled", ["office_location", "office_city"])
async def test_a_row_relabelled_high_after_it_was_read_never_reaches_the_model(
    ch, wired, monkeypatch, relabelled: str
) -> None:
    """The judged row (s2) or its peer (s1) is rewritten with the same bytes and
    sensitivity 'high' after the cluster was read: no call is made."""
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 58)
    session = {"office_location": "s2", "office_city": "s1"}[relabelled]
    value = {"office_location": "The office moved to Rome", "office_city": "The office is in Paris"}
    real = queue.cluster_peers

    async def then_relabel(*a: Any, **kw: Any) -> Any:
        peers = await real(*a, **kw)
        await _set(org, session, relabelled, value[relabelled], sensitivity="high")
        return peers

    monkeypatch.setattr(queue, "cluster_peers", then_relabel)
    out = await run_pass()
    assert llm.calls == [] and out["judged"] == 0
    assert (await _rows(ch, org))["office_city"]["status"] == "active"


@pytest.mark.parametrize("change", ["revised", "pinned"])
async def test_a_flag_never_lands_on_a_peer_changed_after_it_was_judged(
    ch, wired, change: str
) -> None:
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app.update import handle_memory_update_by_id

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 60)
    ids = {k: r["id"] for k, r in (await _rows(ch, org)).items()}
    row = await resolver._head(org, NS, ids["office_location"])
    judged_peer = await resolver._head(org, NS, ids["office_city"])
    if change == "revised":
        await handle_memory_update_by_id(
            MemoryUpdateByIdInput(org=org, memory_id=ids["office_city"], value="Paris, 3rd floor")
        )
    else:
        await _set(org, "s1", "office_city", "The office is in Paris", pinned=True)
    assert await resolver._flag(org, row, judged_peer) is False
    after = await _rows(ch, org)
    assert (
        after["office_city"]["status"] == "active" and after["office_city"]["conflict_with"] == []
    )
    assert after["office_location"]["conflict_with"] == []


async def test_closer_rows_of_another_type_do_not_hide_a_peer(ch, wired) -> None:
    """40 preference vectors sit nearer the queued row than its 0.8 semantic peer."""
    from server.memory.store.embeddings import (
        _copy_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
    )
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 62)
    (row,) = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s2",
        memory_key="office_location",
    )
    closer = [
        _copy_row(
            row,
            {
                "memory_type": "preference",
                "memory_key": f"pref{i}",
                "session_id": "s9",
                "embedding": _vec((62, 0.8), (63, 0.6), (64 + i, 0.01)),
                "conflict_checked_at": None,
            },
        )
        for i in range(40)
    ]
    await insert_embedding_rows(closer, ch=ch)
    try:
        await run_pass()
        assert "office_location" in llm.calls
        assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    finally:
        await _drop_vectors(ch, org, NS, "pref")


def _past_the_writers_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair takes over a fresh FOLLOW_OWED intent, as it does once its writer had
    OWED_GRACE_S to finish."""
    from server.memory.app import projection_failures

    monkeypatch.setattr(projection_failures, "OWED_GRACE_S", 0)


async def _follow_intents(ch: Any, org: str, want_active: int) -> list[dict[str, Any]]:
    # Resolving is a mutation: poll up to ten seconds for `want_active` active rows.
    rows: list[dict[str, Any]] = []
    for _ in range(50):
        rows = await ch.query_all(
            "SELECT status, payload FROM loom.memory_projection_failures"
            " WHERE org = {org:String} AND projection = 'triples_follow_owed'"
            # Not on the fly: the mark sets resolved_at = now(), which cannot be.
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if sum(r["status"] == "active" for r in rows) == want_active:
            break
        await asyncio.sleep(0.2)
    return rows


@pytest.mark.parametrize("follow_fails", [False, True])
async def test_a_retirement_owes_its_follow_until_the_follow_lands(
    ch, wired, monkeypatch, follow_fails: bool
) -> None:
    from server.memory.app import triple_follow
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 66)
    if follow_fails:

        async def boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("graph unavailable")

        monkeypatch.setattr(triple_follow, "follow_row_supersessions", boom)
    out = await run_pass()
    assert out["retired"] == 1
    ids = {k: r["id"] for k, r in (await _rows(ch, org)).items()}
    rows = await _follow_intents(ch, org, want_active=1 if follow_fails else 0)
    assert len(rows) == 1, "one intent, never a second row for the same pair"
    assert rows[0]["status"] == ("active" if follow_fails else "resolved")
    assert json.loads(rows[0]["payload"])["pairs"] == [[ids["office_city"], ids["office_location"]]]
    if follow_fails:
        # The repair replays the owed follow once the graph is back.
        from server.memory.app.projection_failures import FOLLOW_OWED, retry_projection_failures

        monkeypatch.undo()
        _past_the_writers_grace(monkeypatch)
        (retry,) = await retry_projection_failures(org=org, projection=FOLLOW_OWED)
        assert retry.resolved, retry


async def test_a_flagged_pair_is_judged_once_from_both_sides(ch, wired, monkeypatch) -> None:
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    _policy(monkeypatch, "manual_review")  # the update is flagged, not retired
    await _pair(ch, org, "office_city", "office_location", 68)
    out = await run_pass()
    rows = await _rows(ch, org)
    assert out["flagged"] == 1 and rows["office_city"]["status"] == "conflicted"
    marks = await _marks(ch, org)
    assert marks["office_city"] == marks["office_location"] == 2
    calls = len(llm.calls)
    await run_pass()
    assert len(llm.calls) == calls, "the peer's flag is not judged again, reversed"


async def test_a_flagged_peer_of_another_user_is_never_judged_or_retired(
    ch, wired, monkeypatch
) -> None:
    """User u1's owed row is flagged with user u2's row (a cosine flag is namespace
    wide). u2's text never reaches u1's prompt, and u1's update never retires it."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    prompts: list[str] = []
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        prompts.append(user)
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris", user="u2")
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((70, 1.0)), owed=False, user="u2")
    await _place(ch, org, "s2", "office_location", _vec((70, 0.8), (71, 0.6)), owed=True)
    rows = await _rows(ch, org)
    landed = await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["office_location"]["id"],
        peer_ids=[rows["office_city"]["id"]],
        namespace=NS,
    )
    assert landed == "conflict"

    await run_pass()
    assert not any("Paris" in p for p in prompts)
    after = await _rows(ch, org)
    assert after["office_city"]["status"] == "conflicted", "u2's row is not retired"


async def test_a_retirement_waits_for_a_conditional_revision_of_the_peer(
    ch, wired, monkeypatch
) -> None:
    """A conditional revision of the peer holds its key from its read to its insert.
    The resolver does not retire the peer meanwhile; it then sees the revision and
    leaves the pair alone, so the revision is what FINAL serves."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.store.key_write_lock import key_write_locks
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 72)
    peer = (await _rows(ch, org))["office_city"]
    real_locks = resolver._pair_locks
    waiting = asyncio.Event()

    def pair_locks(*a: Any, **kw: Any) -> Any:
        waiting.set()  # the resolver asks for the pair's locks the test holds
        return real_locks(*a, **kw)

    monkeypatch.setattr(resolver, "_pair_locks", pair_locks)
    async with key_write_locks.hold(org, NS, "bot", USER, "s1", "office_city"):
        resolving = asyncio.create_task(run_pass())
        await asyncio.wait_for(waiting.wait(), timeout=30)
        assert (await _rows(ch, org))["office_city"]["status"] == "active"
        # The revision's insert, inside the lock (the lock itself is held by the test).
        await handle_memory_update_by_id(
            MemoryUpdateByIdInput(org=org, memory_id=peer["id"], value="The office is in Lyon")
        )
    out = await resolving
    after = (await _rows(ch, org))["office_city"]
    assert after["status"] == "active" and after["version"] > peer["version"]
    assert out["retired"] == 0 and out["skipped_changed"] == 1


async def test_a_set_on_the_winner_after_the_re_read_retires_nothing(
    ch, wired, monkeypatch
) -> None:
    """memory.set takes no key lock, so a set can land on the winner after the re-read
    under the pair's locks. The supersede reads the winner again and retires nothing
    once it holds another value than the one judged."""
    from server.memory.app import conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1980)
    real_refines = conflicts.apply_refines

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    async def set_then_refine(**kw: Any) -> int:
        # The barrier: the set lands after the re-read, before the supersede.
        with pytest.MonkeyPatch.context() as m:
            m.setattr(conflicts, "check_on_commit", clear)
            await _set(org, "s2", "office_location", "The office moved to Milan")
        return await real_refines(**kw)

    monkeypatch.setattr(conflicts, "apply_refines", set_then_refine)
    out = await run_pass()
    after = await _rows(ch, org)
    assert after["office_city"]["status"] == "active"
    assert after["office_city"]["superseded_by"] == ""
    assert out["retired"] == 0


@pytest.mark.parametrize("clear_fails", [False, True])
async def test_a_retirement_clears_the_winners_flag_against_the_retired_row(
    ch, wired, monkeypatch, clear_fails: bool
) -> None:
    """The write path flagged the pair; the resolver retires the older row. The winner
    is active again, with no flag left, and the audit row says the flag went. A clear
    that fails after the retirement is made by the follow intent's replay."""
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app import conflicts, projection_failures
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 74)
    rows = await _rows(ch, org)
    landed = await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["office_location"]["id"],
        peer_ids=[rows["office_city"]["id"]],
        namespace=NS,
    )
    assert landed == "conflict"
    if clear_fails:
        real = resolver._unlink

        async def fail_once(*a: Any) -> bool:
            monkeypatch.setattr(resolver, "_unlink", real)
            raise RuntimeError("ClickHouse went away")

        monkeypatch.setattr(resolver, "_unlink", fail_once)
        assert (await run_pass())["errored"] == 1
        _past_the_writers_grace(monkeypatch)
        await projection_failures.retry_projection_failures(
            org=org, projection="triples_follow_owed"
        )
        winner = (await _rows(ch, org))["office_location"]
        assert (winner["status"], winner["conflict_with"]) == ("active", [])
        return
    out = await run_pass()
    after = await _rows(ch, org)
    assert out["retired"] == 1 and after["office_city"]["status"] == "superseded"
    winner = after["office_location"]
    assert (winner["status"], winner["conflict_with"]) == ("active", [])
    status = await ch.query_all(
        "SELECT status FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
        " AND memory_key = 'office_location' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["status"] for r in status] == ["active"]
    (acted,) = await _audit(ch, org, "memory.conflict_resolver")
    assert [(p["action"], p.get("flag_cleared")) for p in acted["pairs"]] == [("retire", True)]


@pytest.mark.parametrize("cut_times", [1, 2])
@pytest.mark.parametrize("stop_reason", ["length", ""])
async def test_an_answer_with_nothing_usable_is_asked_again_then_stamped(
    ch, wired, monkeypatch, cut_times: int, stop_reason: str
) -> None:
    """The answer is cut at the output cap, or malformed: the call is made once more
    with twice the room; nothing usable again, the row is stamped judged and counts
    errored, so the next pass does not pay for it again."""
    from types import SimpleNamespace

    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 76)
    real = llm.complete
    caps: list[int] = []

    async def complete(system: str, user: str, **kw: Any) -> Any:
        caps.append(int(kw["max_tokens"]))
        if len(caps) <= cut_times:
            text = '{"peers": [{"peer_id": "p1", "rea'
            return SimpleNamespace(text=text, stop_reason=stop_reason)
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    out = await run_pass()
    assert caps[1] == 2 * caps[0] and len(caps) == 2
    status = (await _rows(ch, org))["office_city"]["status"]
    mark = (await _marks(ch, org))["office_location"]
    if cut_times == 1:
        assert (status, mark, out["errored"]) == ("superseded", 2, 0)
    else:
        assert (status, mark, out["errored"]) == ("active", 2, 1)
    await run_pass()
    assert len(caps) == 2, "a third pass makes no call"


async def test_the_by_id_reads_use_the_memory_id_skip_index(ch, wired) -> None:
    """Each statement that reads rows by id compares memory_id itself, so the bloom
    filter prunes granules (a cast of the column would not)."""
    from server.memory.store import conflict_resolver as queue

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 78)
    ids = [r["id"] for r in (await _rows(ch, org)).values()]
    base = {
        "org": org,
        "ns": NS,
        "ids": ids,
        "a": "bot",
        "u": USER,
        "s": "s2",
        "k": "office_location",
    }
    statements = {
        # The anchored read's id lookup, on its own: EXPLAIN does not show a set's plan.
        "anchored": (queue._FLAGGED_IDENTITIES_SQL, base),
        "protected": (queue._PROTECTED_SQL, base),
        "asserted": (queue._HISTORY_SQL, base | {"rows": [(i, "") for i in ids]}),
    }
    for name, (sql, params) in statements.items():
        plan = await ch.query_all("EXPLAIN indexes = 1 " + sql, params=params)
        assert "memory_id_skip" in " ".join(str(v) for r in plan for v in r.values()), name


async def test_a_value_that_came_back_orders_from_its_return(ch, wired) -> None:
    """The peer said Paris, then Lyon, then Paris again after the judged row was
    written: its value was last asserted after the judged row, so it is not retired."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 80)
    peer = (await _rows(ch, org))["office_city"]["id"]
    await _later()
    for value in ("The office is in Lyon", "The office is in Paris"):
        await handle_memory_update_by_id(
            MemoryUpdateByIdInput(org=org, memory_id=peer, value=value)
        )
    await _place(ch, org, "s1", "office_city", _vec((80, 1.0)), owed=False)
    out = await run_pass()
    assert (await _rows(ch, org))["office_city"]["status"] == "conflicted"
    assert out["retired"] == 0 and out["flagged"] == 1


async def _request_erasure(ch: Any, org: str) -> None:
    import uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    row = {
        "id": str(uuid.uuid4()),
        "org": org,
        "scope_type": "org",
        "scope_value": org,
        "requester": "test",
        "status": "queued",
        "failure_reason": "",
        "requested_at": now,
        "last_checkpoint_at": now,
        "version": 1,
    }
    await ch.insert("loom.erasure_requests", [row])


async def test_an_erasure_requested_during_the_call_stops_the_acting(
    ch, wired, monkeypatch
) -> None:
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 82)
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        await _request_erasure(ch, org)
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    out = await run_pass()
    assert out["judged"] == 1 and out["retired"] == 0
    assert (await _rows(ch, org))["office_city"]["status"] == "active"
    assert (await _marks(ch, org))["office_location"] == 1, "owed, left to after the saga"


@pytest.mark.parametrize("write", ["set", "pin"])
async def test_a_write_that_started_before_a_retirement_stays_the_head(
    ch, wired, monkeypatch, write: str
) -> None:
    """A memory.set or memory.pin of the loser takes no key lock: its version is drawn
    before the resolver writes, and it lands after. The supersede sits at the loser's
    read + 1, below that write, so the user's value (or pin) is what FINAL serves, and
    the overtaken retirement records nothing."""
    import server.memory.store as M
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 84)
    peer_id = (await _rows(ch, org))["office_city"]["id"]
    peer = await _by_id(peer_id, org=org, scope=WHOLE_NAMESPACE, strong=True)
    drawn = M.next_version()  # the user's write, drawn before the resolver acts
    if write == "set":
        late = M.build_memory_version_row(
            peer,
            version=drawn,
            value=json.dumps("The office is in Berlin"),
            content_sha256="b" * 64,
        )
    else:
        late = M.build_memory_version_row(peer, version=drawn, pinned=1)
    real = lifecycle.insert_memory_versions

    async def insert(rows: list[dict[str, Any]], **kw: Any) -> Any:
        out = await real(rows, **kw)
        if any(r.get("status") == "superseded" for r in rows):
            await real([late], **kw)  # lands after the supersede, before its re-read
        return out

    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    out = await run_pass()
    head = (await _rows(ch, org))["office_city"]
    assert head["status"] == "active" and head["version"] == drawn
    if write == "pin":
        assert head["pinned"]
    assert out["retired"] == 0
    assert await _audit(ch, org, "memory.auto_supersede") == []


async def test_an_erasure_requested_while_a_pair_waits_for_its_lock_gets_no_write(
    ch, wired
) -> None:
    """The erasure is read under the pair's locks, right before the write: one requested
    while the resolver waits for the peer's key leaves both rows and the mark as they were."""
    from server.memory.store.key_write_lock import key_write_locks
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 86)
    before = await _rows(ch, org)
    async with key_write_locks.hold(org, NS, "bot", USER, "s1", "office_city"):
        resolving = asyncio.create_task(run_pass())
        await asyncio.sleep(2)
        await _request_erasure(ch, org)
    out = await resolving
    assert out["judged"] == 1 and out["retired"] == 0
    after = await _rows(ch, org)
    assert {k: r["version"] for k, r in after.items()} == {
        k: r["version"] for k, r in before.items()
    }
    assert (await _marks(ch, org))["office_location"] == 1, "owed, left to after the saga"


async def test_an_erasure_requested_once_a_retirement_lands_gets_no_record_or_audit(
    ch, wired, monkeypatch
) -> None:
    """The retirement landed, the erasure came next: its record, follow and audit row,
    written after the locks, are left to the saga (the follow intent stays, and the
    repair leaves it while the erasure runs)."""
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app import projection_failures
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 92)
    real = resolver._settle_locked_pair

    async def then_erase(*a: Any, **kw: Any) -> Any:
        out = await real(*a, **kw)
        await _request_erasure(ch, org)
        return out

    monkeypatch.setattr(resolver, "_settle_locked_pair", then_erase)
    assert (await run_pass())["retired"] == 1
    assert await _audit(ch, org, "memory.auto_supersede") == []
    assert await _audit(ch, org, "memory.conflict_resolver") == []
    assert [r["status"] for r in await _follow_intents(ch, org, want_active=1)] == ["active"]
    _past_the_writers_grace(monkeypatch)
    (retry,) = await projection_failures.retry_projection_failures(
        org=org, projection="triples_follow_owed"
    )
    assert retry.reason == "pending" and not retry.resolved, retry
    assert await _audit(ch, org, "memory.auto_supersede") == []
    assert [r["status"] for r in await _follow_intents(ch, org, want_active=1)] == ["active"]


@pytest.mark.parametrize("earlier", [False, True])
async def test_a_retirement_whose_record_failed_gets_it_from_the_repair(
    ch, wired, monkeypatch, earlier: bool
) -> None:
    """The record's emit fails after the supersede: the follow intent stays active with
    the record's entry, and its replay writes the record the Auto-resolved list reads,
    once. A record of an earlier retirement of the same pair (since undone) is not it."""
    from datetime import UTC, datetime

    from server.insights.api.models_audit import AuditListInput
    from server.insights.app.audit import handle_audit_list
    from server.memory.app import projection_failures
    from server.memory.workers.conflict_resolver import run_pass
    from server.observability import audit

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 88)
    if earlier:
        rows = await _rows(ch, org)
        pair = {
            "memory_id": rows["office_city"]["id"],
            "superseded_by": rows["office_location"]["id"],
        }
        await ch.insert(
            "loom.audit_log",
            [
                {
                    # Seconds before this retirement's intent: an Undo came between.
                    "ts": datetime.now(UTC).replace(tzinfo=None),
                    "event_type": "memory.auto_supersede",
                    "org": org,
                    "namespace": NS,
                    "actor": "process:loom",
                    "subject": json.dumps({"earlier": True, "retired": [pair]}),
                    "request_id": "",
                }
            ],
        )
    real = audit.emit

    async def emit(event_type: str, **kw: Any) -> str:
        if event_type == "memory.auto_supersede":
            return ""  # as audit.emit answers when its insert fails
        return await real(event_type, **kw)

    monkeypatch.setattr(audit, "emit", emit)
    assert (await run_pass())["retired"] == 1
    monkeypatch.setattr(audit, "emit", real)
    assert len(await _audit(ch, org, "memory.auto_supersede")) == earlier
    (intent,) = await _follow_intents(ch, org, want_active=1)
    assert intent["status"] == "active"

    _past_the_writers_grace(monkeypatch)
    for _ in range(2):  # a second replay writes no second record
        await projection_failures.retry_projection_failures(
            org=org, projection="triples_follow_owed"
        )
    rows = await _rows(ch, org)
    records = await _audit(ch, org, "memory.auto_supersede")
    (record,) = [r for r in records if "earlier" not in r]
    (entry,) = record["retired"]
    assert record["reason"] == "conflict_resolver"
    assert (entry["memory_id"], entry["superseded_by"]) == (
        rows["office_city"]["id"],
        rows["office_location"]["id"],
    )
    listed = await handle_audit_list(
        AuditListInput(org=org, namespace=NS, event_type="memory.auto_supersede")
    )
    assert len(records) == len(listed.rows) == 1 + earlier
    assert [r["status"] for r in await _follow_intents(ch, org, want_active=0)] == ["resolved"]


async def test_a_retirement_and_its_undo_leave_a_users_supersedes_link_as_it_was(ch, wired) -> None:
    """The retirement's lineage is the row's superseded_by and its record: the key-level
    edge store, which holds the user's own links, is not written or rescinded."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.api.models_graph import MemoryLinkInput
    from server.memory.app.ops import handle_memory_link
    from server.memory.app.restore import handle_memory_restore
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 90)
    await handle_memory_link(
        MemoryLinkInput(
            org=org,
            namespace=NS,
            from_key="office_location",
            to_key="office_city",
            relation="supersedes",
            explanation="the user's own link",
        )
    )

    async def edges() -> list[dict[str, Any]]:
        return await ch.query_all(
            "SELECT explanation, tombstoned_at FROM loom.operator_edges FINAL"
            " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )

    linked = await edges()
    assert (await run_pass())["retired"] == 1
    await asyncio.sleep(1.1)  # an edge's version is its created_at, to the second
    rows = await _rows(ch, org)
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=rows["office_city"]["superseded_by"],
        )
    )
    assert undone.restored
    assert (
        await edges() == linked == [{"explanation": "the user's own link", "tombstoned_at": None}]
    )


async def test_a_peer_flagged_by_another_rows_turn_is_still_judged_against_its_own_peers(
    ch, wired, monkeypatch
) -> None:
    """A-B at 0.8, B-C at 0.8, A-C at 0.28; A and B owed. A's turn flags A-B; B keeps its
    own mark, so its turn judges it against C, which A's cluster never held."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass
    from server.settings import settings

    org, llm = wired
    monkeypatch.setitem(_SCRIPT, "e2_a", ("contradiction", 0.97))

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        # A written last: the owed read takes a namespace's newest rows first.
        for session, key in (("s2", "e2_b"), ("s3", "e2_c"), ("s1", "e2_a")):
            await _set(org, session, key, f"The {key} note")
            await _later()
        await after_write.drain()
    await _place(ch, org, "s1", "e2_a", _vec((92, 1.0)), owed=True)
    await _place(ch, org, "s2", "e2_b", _vec((92, 0.8), (93, 0.6)), owed=True)
    await _place(ch, org, "s3", "e2_c", _vec((92, 0.28), (93, 0.96)), owed=False)
    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 1)
    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    for _ in range(3):
        await run_pass()
    assert llm.calls[:2] == ["e2_a", "e2_b"], llm.calls
    assert (await _marks(ch, org))["e2_b"] == 2


async def test_a_repair_before_the_supersede_commits_leaves_the_follow_owed(
    ch, wired, monkeypatch
) -> None:
    """The repair runs between the intent and the supersede: the loser is still live, so
    it leaves the intent alone. The supersede commits, the resolver's follow fails, and
    the next repair follows the pair."""
    from server.memory.app import after_write, conflicts, triple_follow
    from server.memory.app.projection_failures import FOLLOW_OWED, retry_projection_failures
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        office = ("office", "located_in", "paris")
        await _set(org, "s1", "office_city", "The office is in Paris", relations=[office])
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((94, 1.0)), owed=False)
    await _place(ch, org, "s2", "office_location", _vec((94, 0.8), (95, 0.6)), owed=True)
    assert await _paris_open(ch, org, want=True)
    refines, follow = conflicts.apply_refines, triple_follow.follow_row_supersessions
    early: list[Any] = []

    async def repair_first(**kw: Any) -> Any:
        early.extend(await retry_projection_failures(org=org, projection=FOLLOW_OWED))
        return await refines(**kw)

    async def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(conflicts, "apply_refines", repair_first)
    monkeypatch.setattr(triple_follow, "follow_row_supersessions", boom)
    assert (await run_pass())["retired"] == 1
    assert [(r.retried, r.reason) for r in early] == [(False, "pending")]
    (intent,) = await _follow_intents(ch, org, want_active=1)
    assert intent["status"] == "active"

    monkeypatch.setattr(triple_follow, "follow_row_supersessions", follow)
    _past_the_writers_grace(monkeypatch)
    (retry,) = await retry_projection_failures(org=org, projection=FOLLOW_OWED)
    assert retry.resolved, retry
    assert not await _paris_open(ch, org, want=False)


async def test_a_key_held_in_many_sessions_is_read_within_bounds(ch, wired, monkeypatch) -> None:
    """300 other sessions hold the row's key: the same-key read is bounded in SQL, so
    the peer-rows read gets a handful of ids (not one per session, which at thousands
    passes the HTTP form field limit) and the row is judged."""
    from server.core.ch import get_client
    from server.memory.store import conflict_resolver as queue
    from server.memory.store.embeddings import (
        _copy_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
    )
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 96)
    (row,) = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s2",
        memory_key="office_location",
    )
    sessions = [
        _copy_row(
            row,
            {"session_id": f"many{i}", "embedding": _vec((97, 1.0)), "conflict_checked_at": None},
        )
        for i in range(300)
    ]
    await insert_embedding_rows(sessions, ch=ch)
    client = get_client()
    real_query_all, sent = client.query_all, []

    async def query_all(sql: str, params: Any = None, **kw: Any) -> Any:
        if sql is queue._PEER_ROWS_SQL:
            sent.append(len(params["ids"]))
        return await real_query_all(sql, params, **kw)

    monkeypatch.setattr(client, "query_all", query_all)
    try:
        out = await run_pass()
        assert out["errored"] == 0 and llm.calls == ["office_location"], out
        assert sent and max(sent) < 50, sent
    finally:
        monkeypatch.undo()
        await _drop_vectors(ch, org, NS, "office_location")


async def test_high_sensitivity_rows_of_the_key_do_not_take_the_live_rows_place(
    ch, wired, monkeypatch
) -> None:
    """Five rows of the key labelled 'high', which no prompt shows, sit nearer than its
    live row of s1, which is below the floor: the live row is still judged."""
    from server.memory.app import after_write
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    for i in range(5):
        await _set(org, f"high{i}", "office", f"The office code is {i}")
    await _pair(ch, org, "office", "office", 144)
    await after_write.drain()
    await _place(ch, org, "s1", "office", _vec((154, 1.0)), owed=False)
    for i in range(5):
        near = _vec((144, 0.8), (145, 0.6), (146 + i, 0.01))
        await _place(ch, org, f"high{i}", "office", near, owed=False)
        # Relabelled with the same bytes: the vector row stays as it was.
        await _set(org, f"high{i}", "office", f"The office code is {i}", sensitivity="high")
    await after_write.drain()
    prompts: list[str] = []
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        prompts.append(user)
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    try:
        await run_pass()
        assert llm.calls == ["office"] and "Paris" in prompts[0]
        assert "office code" not in prompts[0]
    finally:
        await _drop_vectors(ch, org, NS, "office")


async def test_dead_rows_of_the_key_do_not_take_the_live_rows_place(ch, wired, monkeypatch) -> None:
    """Six vector rows of the key that a peer read drops (superseded, expired, past
    valid_to, another type) sit nearer than its live row of s1, which is below the
    floor: the live row is still judged."""
    from datetime import UTC, datetime, timedelta

    from server.memory.store.embeddings import (
        _copy_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
    )
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office", "office", 140)
    await _place(ch, org, "s1", "office", _vec((150, 1.0)), owed=False)
    (row,) = await live_embedding_rows_for_key(
        ch, org=org, namespace=NS, agent="bot", user_id=USER, session_id="s2", memory_key="office"
    )
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    dead = [
        {"status": "superseded"},
        {"status": "superseded"},
        {"expires_at": past},
        {"valid_to": past},
        {"memory_type": "preference"},
        {"memory_type": "preference"},
    ]
    copies = [
        _copy_row(row, {**d, "session_id": f"dead{i}", "conflict_checked_at": None})
        for i, d in enumerate(dead)
    ]
    await insert_embedding_rows(copies, ch=ch)
    prompts: list[str] = []
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        prompts.append(user)
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    try:
        await run_pass()
        assert llm.calls == ["office"] and "Paris" in prompts[0]
    finally:
        await _drop_vectors(ch, org, NS, "office")


@pytest.mark.parametrize(
    ("policy", "confidence", "want"),
    [
        ("auto_reinforce", 0.98, "retired"),
        ("auto_reinforce", 0.97, "flagged"),
        ("auto_reinforce", 0.95, "flagged"),
        ("auto_reinforce", 0.94, "unsure"),
        ("manual_review", 0.99, "flagged"),
        ("append_only", 0.99, None),
    ],
)
async def test_the_policy_decides_whether_an_update_retires_or_flags(
    ch, wired, monkeypatch, policy: str, confidence: float, want: str | None
) -> None:
    """The default policy retires the older row on a confident update, as newest_wins
    does; below the retire confidence, and under manual_review, the pair is flagged;
    under append_only nothing is judged."""
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    _policy(monkeypatch, policy)
    monkeypatch.setitem(_SCRIPT, "office_location", ("update", confidence))
    await _pair(ch, org, "office_city", "office_location", 200)
    before = await _rows(ch, org)
    out = await run_pass()
    rows = await _rows(ch, org)
    old = rows["office_city"]
    records = await _audit(ch, org, "memory.auto_supersede")
    if want == "retired":
        assert out["retired"] == 1 and out["flagged"] == 0, out
        assert (old["status"], old["superseded_by"]) == (
            "superseded",
            rows["office_location"]["id"],
        )
        (record,) = records
        assert record["reason"] == "conflict_resolver"
        assert [e["memory_id"] for e in record["retired"]] == [old["id"]]
    elif want == "flagged":
        assert out["retired"] == 0 and out["flagged"] == 1, out
        assert old["status"] == "conflicted" and records == []
    elif want == "unsure":
        # Judged, but below the flag confidence: nothing changes.
        assert out["judged"] == 1 and out["retired"] == out["flagged"] == 0, out
        assert old["status"] == "active" and records == []
    else:
        assert llm.calls == [] and out["retired"] == out["flagged"] == 0, out
        assert {k: (r["status"], r["version"]) for k, r in rows.items()} == {
            k: (r["status"], r["version"]) for k, r in before.items()
        }
        assert records == []


async def test_an_undone_retirement_is_not_retired_again_by_the_next_pass(
    ch, wired, monkeypatch
) -> None:
    """Under the default policy the resolver retires the older row; the Undo brings it
    back, and the next pass neither judges the pair again nor retires it."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    _policy(monkeypatch, "auto_reinforce")
    await _pair(ch, org, "office_city", "office_location", 202)
    assert (await run_pass())["retired"] == 1
    rows = await _rows(ch, org)
    winner = rows["office_location"]["id"]
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=winner,
        )
    )
    assert undone.restored
    calls = len(llm.calls)
    out = await run_pass()
    after = await _rows(ch, org)
    assert after["office_city"]["status"] == "active", "the Undo holds"
    assert out["judged"] == 0 and out["retired"] == 0 and len(llm.calls) == calls, out
    assert len(await _audit(ch, org, "memory.auto_supersede")) == 1


async def test_a_pin_racing_a_retirement_waits_for_the_key_and_ends_served(
    ch, wired, monkeypatch
) -> None:
    """A memory.pin of the loser arrives while the resolver retires it under the pair's
    key locks: the pin waits for the lock, lands after the retirement, and the pinned
    row and its vector are both served."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app.handlers import handle_memory_pin
    from server.memory.store.embeddings import live_embedding_rows_for_key
    from server.memory.store.key_write_lock import key_write_locks
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 204)
    ident = {"agent": "bot", "user_id": USER, "session_id": "s1", "memory_key": "office_city"}
    real = resolver._retire
    real_hold = key_write_locks.hold
    pins: list[asyncio.Task[Any]] = []
    pin_waits = asyncio.Event()

    def hold(*a: Any, **kw: Any) -> Any:
        pin_waits.set()  # only the pin asks for a lock once the retirement holds both
        return real_hold(*a, **kw)

    async def retire_beside_a_pin(*a: Any, **kw: Any) -> Any:
        pin = MemorySetInput(org=org, namespace=NS, **ident, value="The office is in Paris")
        with monkeypatch.context() as m:
            m.setattr(key_write_locks, "hold", hold)
            pins.append(asyncio.create_task(handle_memory_pin(pin)))
            await asyncio.wait_for(pin_waits.wait(), timeout=30)
        await asyncio.sleep(0)
        assert not pins[0].done(), "the pin waits for the key's lock"
        return await real(*a, **kw)

    monkeypatch.setattr(resolver, "_retire", retire_beside_a_pin)
    assert (await run_pass())["retired"] == 1
    await pins[0]
    await after_write.drain()
    head = (await _rows(ch, org))["office_city"]
    assert head["pinned"] and head["status"] == "active", head
    (served,) = await live_embedding_rows_for_key(ch, org=org, namespace=NS, **ident)
    assert served["status"] == "active" and served["invalid_at"] is None


async def test_a_supersede_whose_response_was_lost_is_still_followed(
    ch, wired, monkeypatch
) -> None:
    """The supersede commits, but its insert call raises (a lost response): the row is
    retired, so its record and its graph follow are still made."""
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _seed(ch, org, monkeypatch)
    real = lifecycle.insert_memory_versions

    async def lost(rows: list[dict[str, Any]], **kw: Any) -> Any:
        out = await real(rows, **kw)
        if any(r.get("status") == "superseded" for r in rows):
            raise ConnectionError("the response was lost")
        return out

    monkeypatch.setattr(lifecycle, "insert_memory_versions", lost)
    await run_pass()
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    assert not await _paris_open(ch, org, False), "the retired row's fact is closed"
    assert len(await _audit(ch, org, "memory.auto_supersede")) == 1


async def test_a_date_corrected_during_the_call_decides_the_order(ch, wired, monkeypatch) -> None:
    """Paris (2024) is older than Rome (2025) when the call starts; its date is corrected to
    2026 during the call, with the same bytes: Paris is now newer and is not retired."""
    from datetime import datetime

    import server.memory.store as M
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 214)

    async def dated(key: str, when: datetime) -> None:
        mid = (await _rows(ch, org))[key]["id"]
        row = await _by_id(mid, org=org, scope=WHOLE_NAMESPACE, strong=True)
        moved = M.build_memory_version_row(row, version=M.next_version(), valid_at=when)
        await lifecycle.insert_memory_versions([moved], ch=ch)

    await dated("office_city", datetime(2024, 1, 1))
    await dated("office_location", datetime(2025, 1, 1))
    real = llm.complete

    async def complete(system: str, user: str, **kw: Any) -> Any:
        await dated("office_city", datetime(2026, 1, 1))
        return await real(system, user, **kw)

    monkeypatch.setattr(llm, "complete", complete)
    out = await run_pass()
    assert out["retired"] == 0 and out["flagged"] == 1, out
    assert (await _rows(ch, org))["office_city"]["status"] == "conflicted"


async def test_a_row_flagged_against_thousands_of_rows_is_still_judged(ch, wired) -> None:
    """The row's conflict_with names 4,000 ids, one of them its live peer: the cluster
    reads the flags in ClickHouse, bounded, and the update is judged."""
    import uuid

    import server.memory.store as M
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 218)
    rows = await _rows(ch, org)
    flags = [rows["office_city"]["id"]] + [str(uuid.uuid4()) for _ in range(3999)]
    row = await _by_id(rows["office_location"]["id"], org=org, scope=WHOLE_NAMESPACE, strong=True)
    flagged = M.build_memory_version_row(
        row, version=M.next_version(), status="conflicted", conflict_with=flags
    )
    await lifecycle.insert_memory_versions([flagged], ch=ch)
    out = await run_pass()
    assert out["errored"] == 0 and llm.calls == ["office_location"], out
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"


async def test_the_resolver_stops_at_its_share_of_the_cap_within_one_round_in_flight(
    ch, wired, monkeypatch
) -> None:
    """A namespace capped at $0.40 a day holds 30 owed rows, each judged by a call that
    bills about $0.0106 (1,300 tokens in, 800 out on gpt-6.1-sol) and takes 0.5 s: $0.32
    in all, under the cap. The resolver may spend its share of the cap (a quarter), read
    from loom.agent_events as in production: it stops within one round of calls in
    flight past $0.10, the rest stay owed, the next pass leaves the namespace out of its
    read, and the requests still have the rest of the cap."""
    from datetime import UTC, datetime

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass
    from server.observability import cost_accumulator
    from server.observability.events import get_emitter
    from server.settings import settings

    org, llm = wired
    cap, rows_n, concurrency = 0.40, 30, int(settings.memory_conflict_resolver_concurrency)
    share = float(settings.memory_conflict_resolver_cap_share)
    per_call = cost_accumulator.llm_cost_usd("gpt-6.1-sol", 1300, 800)
    scripted = llm.complete

    async def billed(system: str, user: str, **kw: Any) -> Any:
        await asyncio.sleep(0.5)
        cost_accumulator.report_llm("gpt-6.1-sol", 1300, 800)
        return await scripted(system, user, **kw)

    monkeypatch.setattr(llm, "complete", billed)
    now = datetime.now(UTC)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": NS,
                "created_at": now,
                "updated_at": now,
                "active": True,
                "daily_cost_cap_usd": cap,
            }
        ],
    )

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        for i in range(rows_n):
            await _set(org, f"s{i}", f"note_{i}", f"The user note number {i}")
        await after_write.drain()
    for i in range(rows_n):
        await _place(ch, org, f"s{i}", f"note_{i}", _vec((40, 1.0), (41 + i % 5, 0.3)), owed=True)

    async def spent() -> float:
        await get_emitter().flush_now()
        rows = await ch.query_all(
            "SELECT toFloat64(sum(upstream_cost_usd)) AS usd, sum(llm_calls) AS calls"
            " FROM loom.agent_events WHERE org = {org:String} AND namespace = {ns:String}"
            " AND operation = 'worker.conflict_resolver'",
            params={"org": org, "ns": NS},
        )
        return float(rows[0]["usd"] or 0.0)

    await get_emitter().start()
    try:
        first = await run_pass()
        after_first = await spent()
        second = await run_pass()
        after_second = await spent()
    finally:
        await get_emitter().flush_now()
    marks = await _marks(ch, org)
    owed = sum(1 for v in marks.values() if v == 1)
    assert after_first >= share * cap, "the pass ran until its share was crossed"
    assert after_first <= share * cap + concurrency * per_call + 1e-9, "one round past it"
    assert first["skipped_budget"] > 0 and owed > 0
    assert after_second == after_first, "the next pass spends nothing"
    assert second["capped_namespaces"] >= 1 and second["judged"] == 0
    # A request reads the whole spend against the whole cap: it is not capped.
    from server.observability.budget import namespace_budget_ratio, namespace_over_cap

    assert not await namespace_over_cap(org, NS)
    assert await namespace_budget_ratio(org, NS) < 1


async def test_a_flag_against_a_row_no_longer_live_is_cleared(ch, wired) -> None:
    """office_location is flagged against office_city, retired since: a retired row never
    joins a cluster, so no judgement would clear the flag. The pass clears it, with the
    clear's audit row, and the row is active again."""
    import server.memory.store as M
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.app.conflict_resolver import AUDIT_EVENT
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 230)
    rows = await _rows(ch, org)
    old_id, new_id = rows["office_city"]["id"], rows["office_location"]["id"]
    old = await _by_id(old_id, org=org, scope=WHOLE_NAMESPACE, strong=True)
    new = await _by_id(new_id, org=org, scope=WHOLE_NAMESPACE, strong=True)
    await lifecycle.insert_memory_versions(
        [
            M.build_memory_version_row(
                old, version=M.next_version(), status="superseded", superseded_by=new_id
            ),
            M.build_memory_version_row(
                new, version=M.next_version(), status="conflicted", conflict_with=[old_id]
            ),
        ],
        ch=ch,
    )
    out = await run_pass()
    assert out["errored"] == 0 and out["cleared"] == 1, out
    assert llm.calls == [], "nothing live is left to judge it against"
    row = (await _rows(ch, org))["office_location"]
    assert (row["status"], list(row["conflict_with"])) == ("active", [])
    (audit,) = await _audit(ch, org, AUDIT_EVENT)
    assert audit["memory_id"] == new_id
    assert audit["pairs"] == [{"peer_id": old_id, "action": "clear", "reason": "peer_not_live"}]
    assert (await run_pass())["cleared"] == 0


async def test_a_peer_embedded_by_another_model_of_the_same_dimension_is_not_compared(
    ch, wired
) -> None:
    """office_city's vector is another model's, of the same dimension: its cosine with
    office_location's means nothing, so it is no peer and nothing is judged."""
    from datetime import UTC, datetime

    from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 240)
    (old,) = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key="office_city",
    )
    await reemit_rows([old], {"embedding_model": "other-embedder"}, ch=ch)
    await reemit_rows([old], {"tombstoned_at": datetime.now(UTC)}, ch=ch)
    out = await run_pass()
    assert llm.calls == [] and out["errored"] == 0, out
    assert (await _rows(ch, org))["office_city"]["status"] == "active"


async def test_a_namespace_whose_rows_are_all_newer_still_gets_a_place(ch, wired) -> None:
    """Three namespaces owe a row each, c's the newest, and a pass reads two: ordered by
    age within the turn, a and b would take both places every pass. The order within a
    turn comes from the pass's seed, so some pass reads c."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.write import handle_memory_set
    from server.memory.store import conflict_resolver as queue
    from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows

    org, _ = wired
    for ns in ("turn-a", "turn-b", "turn-c"):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=ns,
                agent="bot",
                user_id=USER,
                session_id="s1",
                memory_key="k",
                value=f"note in {ns}",
            )
        )
        await _later()
    await after_write.drain()
    for ns in ("turn-a", "turn-b", "turn-c"):
        rows = await live_embedding_rows_for_key(
            ch, org=org, namespace=ns, agent="bot", user_id=USER, session_id="s1", memory_key="k"
        )
        await reemit_rows(rows, {"conflict_checked_at": queue.OWED_MARK}, ch=ch)
    served = []
    for seed in range(16):
        picked = {r["namespace"] for r in await queue.owed_rows(limit=2, seed=seed)}
        assert len(picked) == 2, picked
        served.append(picked)
    assert any("turn-c" in p for p in served), "the newest namespace is never read"


async def test_two_overlapping_passes_retire_record_and_audit_a_pair_once(ch, wired) -> None:
    """A leader handoff overlaps two passes on one owed row: one retirement, one record,
    one audit row of the judgement that acted."""
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 250)
    first, second = await asyncio.gather(run_pass(), run_pass())
    assert first["retired"] + second["retired"] == 1, (first, second)
    rows = await _rows(ch, org)
    assert rows["office_city"]["status"] == "superseded"
    assert len(await _audit(ch, org, "memory.auto_supersede")) == 1
    acted = await _audit(ch, org, "memory.conflict_resolver")
    assert [p["action"] for a in acted for p in a["pairs"]] == ["retire"]


async def test_a_pass_that_dies_after_the_supersede_is_finished_by_the_next_and_the_repair(
    ch, wired, monkeypatch
) -> None:
    """The process stops right after the supersede: no record, no follow, no stamp. The
    next pass judges the row again and retires nothing more; the repair writes the
    record from the intent, once."""
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app.projection_failures import FOLLOW_OWED, retry_projection_failures
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 252)

    async def gone(*a: Any, **kw: Any) -> Any:
        return 0

    with monkeypatch.context() as m:
        m.setattr(resolver, "_after_retire", gone)
        m.setattr(queue, "stamp_judged", gone)
        assert (await run_pass())["retired"] == 1
    assert await _audit(ch, org, "memory.auto_supersede") == []
    assert (await _marks(ch, org))["office_location"] == 1, "still owed"
    again = await run_pass()
    assert again["retired"] == 0, again
    _past_the_writers_grace(monkeypatch)
    for _ in range(2):
        await retry_projection_failures(org=org, projection=FOLLOW_OWED)
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    assert len(await _audit(ch, org, "memory.auto_supersede")) == 1
    assert len(await _audit(ch, org, "memory.conflict_resolver")) == 1


async def test_an_update_across_an_agents_sessions_with_no_user_changes_nothing(
    ch, wired, monkeypatch
) -> None:
    """With no real user (`-`), the subject is one agent, whose sessions are separate
    tasks: a verdict across them could only clear a flag, and the pair has none, so no
    call is paid for and nothing changes."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris", user="-")
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome", user="-")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((260, 1.0)), owed=False, user="-")
    await _place(
        ch, org, "s2", "office_location", _vec((260, 0.8), (261, 0.6)), owed=True, user="-"
    )
    out = await run_pass()
    assert llm.calls == [], "nothing a call could change"
    assert out["retired"] == out["flagged"] == out["judged"] == 0, out
    rows = await _rows(ch, org)
    assert (rows["office_city"]["status"], rows["office_city"]["conflict_with"]) == ("active", [])


async def test_an_undo_holds_while_the_winner_is_still_owed(ch, wired, monkeypatch) -> None:
    """The pass dies after the supersede (no stamp), the repair records it and a person
    undoes it: the winner is still owed, and the next pass leaves the pair alone."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import conflict_resolver as resolver
    from server.memory.app.projection_failures import FOLLOW_OWED, retry_projection_failures
    from server.memory.app.restore import handle_memory_restore
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 300)

    async def gone(*a: Any, **kw: Any) -> Any:
        return 0

    with monkeypatch.context() as m:
        m.setattr(resolver, "_after_retire", gone)
        m.setattr(queue, "stamp_judged", gone)
        assert (await run_pass())["retired"] == 1
    _past_the_writers_grace(monkeypatch)
    await retry_projection_failures(org=org, projection=FOLLOW_OWED)
    rows = await _rows(ch, org)
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=rows["office_location"]["id"],
        )
    )
    assert undone.restored
    again = await run_pass()
    assert again["retired"] == 0 and again["skipped_no_action"] >= 1, again
    assert (await _rows(ch, org))["office_city"]["status"] == "active"


async def test_an_undo_holds_while_a_left_out_flag_keeps_the_winner_owed(
    ch, wired, monkeypatch
) -> None:
    """The model always leaves out the peer the winner is flagged against, so the winner
    stays owed and is judged every pass: after an Undo, no pass retires the row again."""
    from types import SimpleNamespace

    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import after_write, conflicts
    from server.memory.app.restore import handle_memory_restore
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def complete(system: str, user: str, **kw: Any) -> Any:
        llm.calls.append("x")
        update = "NEW note (key office_location," in user
        entries = [
            {
                "peer_id": pid,
                "reason": "scripted",
                "same_entity": update,
                "same_attribute": update,
                "verdict": "update" if update else "unrelated",
                "confidence": 0.99 if update else 0.5,
            }
            for pid, key in re.findall(r"^\[(p\d+)\] \(key ([^,]+),", user, re.M)
            if key != "lead_name"  # the flagged peer is always left out
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))

    monkeypatch.setattr(llm, "complete", complete)

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await _set(org, "s1", "lead_name", "The team lead is Ana")
        await _later()
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((400, 1.0)), owed=False)
    await _place(ch, org, "s1", "lead_name", _vec((400, 0.9), (402, 0.436)), owed=False)
    await _place(ch, org, "s2", "office_location", _vec((400, 0.8), (401, 0.6)), owed=True)
    rows = await _rows(ch, org)
    landed = await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["office_location"]["id"],
        peer_ids=[rows["lead_name"]["id"]],
        namespace=NS,
    )
    assert landed == "conflict"
    assert (await run_pass())["retired"] == 1
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=rows["office_location"]["id"],
        )
    )
    assert undone.restored
    for _ in range(2):
        assert (await run_pass())["retired"] == 0
    assert (await _rows(ch, org))["office_city"]["status"] == "active"


async def test_a_flagged_peer_the_model_keeps_leaving_out_stops_costing_calls(
    ch, wired, monkeypatch
) -> None:
    from types import SimpleNamespace

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers import conflict_resolver as worker

    org, llm = wired
    monkeypatch.setattr(worker, "_state", worker._State())

    async def complete(system: str, user: str, **kw: Any) -> Any:
        llm.calls.append("x")
        left_out = "NEW note (key office_location," in user
        entries = [
            {
                "peer_id": pid,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": "related",
                "confidence": 0.5,
            }
            for pid, key in re.findall(r"^\[(p\d+)\] \(key ([^,]+),", user, re.M)
            if not (left_out and key == "lead_name")
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))

    monkeypatch.setattr(llm, "complete", complete)

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await _set(org, "s1", "lead_name", "The team lead is Ana")
        await _set(org, "s2", "office_location", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((500, 1.0)), owed=False)
    await _place(ch, org, "s1", "lead_name", _vec((500, 0.9), (502, 0.436)), owed=False)
    await _place(ch, org, "s2", "office_location", _vec((500, 0.8), (501, 0.6)), owed=True)
    rows = await _rows(ch, org)
    await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["office_location"]["id"],
        peer_ids=[rows["lead_name"]["id"]],
        namespace=NS,
    )
    calls = []
    for _ in range(5):
        before = len(llm.calls)
        await worker.run_pass()
        calls.append(len(llm.calls) - before)
    assert (
        calls[-1] == 0
        and sum(calls) <= 2 * worker.settings.memory_conflict_resolver_max_attempts + 2
    ), calls
    assert (await _marks(ch, org))["office_location"] == 2


async def test_a_request_refused_for_every_row_stamps_nothing_pass_after_pass(
    ch, wired, monkeypatch
) -> None:
    import httpx

    from server.memory.workers.conflict_resolver import run_pass
    from server.settings import settings

    org, llm = wired
    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    for i in range(6):
        await _pair(ch, org, f"old{i}", f"new{i}", 600 + 4 * i, gap=False)

    async def refused(system: str, user: str, **kw: Any) -> Any:
        llm.calls.append("x")
        req = httpx.Request("POST", "https://llm.example/v1")
        raise httpx.HTTPStatusError(
            "400 Bad Request", request=req, response=httpx.Response(400, request=req)
        )

    monkeypatch.setattr(llm, "complete", refused)
    for _ in range(3):
        await run_pass()
    marks = await _marks(ch, org)
    assert sum(1 for v in marks.values() if v == 2) == 0, marks
    assert sum(1 for v in marks.values() if v == 1) == 6


async def test_a_namespaces_newest_owed_row_is_judged_first(ch, wired, monkeypatch) -> None:
    """A namespace owes 30 older rows and one fresh update; a pass reads five. Under its
    cost cap a busy namespace may never drain: its newest write is judged first."""
    from datetime import timedelta

    from server.memory.store.conflict_resolver import OWED_MARK
    from server.memory.store.embeddings import (
        _copy_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
    )
    from server.memory.workers import conflict_resolver as worker
    from server.memory.workers.conflict_resolver import run_pass
    from server.settings import settings

    org, llm = wired
    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 5)
    monkeypatch.setattr(
        worker.settings, "memory_conflict_resolver_burst_seconds", 0.0
    )  # one batch a pass
    await _pair(ch, org, "office_city", "office_location", 270)
    (row,) = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s2",
        memory_key="office_location",
    )
    older = row["created_at"] - timedelta(hours=1)
    history = [
        _copy_row(
            row,
            {
                "memory_key": f"old_{i}",
                "created_at": older + timedelta(seconds=i),
                "conflict_checked_at": OWED_MARK,
            },
        )
        for i in range(30)
    ]
    await insert_embedding_rows(history, ch=ch)
    await run_pass()
    assert llm.calls == ["office_location"], llm.calls


async def test_an_undo_holds_only_while_the_winner_holds_its_value(ch, wired, monkeypatch) -> None:
    """Rome retires Paris and a person undoes it. Rome's writer later revises it to
    Berlin (the same row): Berlin is a new statement, judged against Paris, and retires it."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import after_write, conflicts
    from server.memory.app.restore import handle_memory_restore
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1400)
    assert (await run_pass())["retired"] == 1
    rows = await _rows(ch, org)
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=rows["office_location"]["id"],
        )
    )
    assert undone.restored
    await _later()

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s2", "office_location", "The office moved to Berlin")
        await after_write.drain()
    await _place(ch, org, "s2", "office_location", _vec((1400, 0.8), (1401, 0.6)), owed=True)
    assert (await _rows(ch, org))["office_location"]["id"] == rows["office_location"]["id"]
    assert (await run_pass())["retired"] == 1
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"


async def test_an_undo_is_not_flagged_from_the_other_side(ch, wired, monkeypatch) -> None:
    """Rome retires Paris, whose own turn had not come, and a person undoes it. Paris's
    turn then judges Rome, told it is the later statement, as a contradiction: the pair a
    person settled is left alone, not flagged."""
    from types import SimpleNamespace

    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore
    from server.memory.workers import conflict_resolver as worker
    from server.settings import settings

    org, llm = wired
    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 1)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_burst_seconds", 0.0)

    async def complete(system: str, user: str, **kw: Any) -> Any:
        update = "NEW note (key office_location," in user
        entries = [
            {
                "peer_id": pid,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": "update" if update else "contradiction",
                "confidence": 0.98,
            }
            for pid in re.findall(r"^\[(p\d+)\]", user, re.M)
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))

    monkeypatch.setattr(llm, "complete", complete)
    await _pair(ch, org, "office_city", "office_location", 1100)
    await _place(ch, org, "s1", "office_city", _vec((1100, 1.0)), owed=True)
    assert (await worker.run_pass())["retired"] == 1
    rows = await _rows(ch, org)
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=rows["office_location"]["id"],
        )
    )
    assert undone.restored
    out = await worker.run_pass()
    assert out["flagged"] == 0, out
    after = await _rows(ch, org)
    assert after["office_city"]["status"] == after["office_location"]["status"] == "active"


async def test_a_row_left_owed_is_judged_once_a_pass_whatever_the_burst_reads(
    ch, wired, monkeypatch
) -> None:
    """Batches of two over a backlog, and one row whose flagged peer the model always
    leaves out: the row stays owed, and the burst's next read takes it again; it is
    judged, and paid for, once in the pass."""
    from types import SimpleNamespace

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers import conflict_resolver as worker
    from server.settings import settings

    org, llm = wired
    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 2)
    calls: list[str] = []

    async def complete(system: str, user: str, **kw: Any) -> Any:
        new = re.search(r"NEW note \(key ([^,)]+)", user).group(1)  # type: ignore[union-attr]
        calls.append(new)
        entries = [
            {
                "peer_id": pid,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": "related",
                "confidence": 0.5,
            }
            for pid, key in re.findall(r"^\[(p\d+)\] \(key ([^,]+),", user, re.M)
            if not (new == "leftout_row" and key == "leftout_flag")
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))

    monkeypatch.setattr(llm, "complete", complete)
    for i in range(6):
        await _pair(ch, org, f"old{i}", f"new{i}", 920 + 4 * i, gap=False)

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "leftout_flag", "The team lead is Ana")
        await _set(org, "s1", "leftout_near", "Ana joined in May")
        await _later()
        await _set(org, "s2", "leftout_row", "Ana leads the team now")
        await after_write.drain()
    await _place(ch, org, "s1", "leftout_flag", _vec((990, 1.0)), owed=False)
    await _place(ch, org, "s1", "leftout_near", _vec((990, 0.9), (992, 0.43)), owed=False)
    await _place(ch, org, "s2", "leftout_row", _vec((990, 0.9), (991, 0.43)), owed=True)
    rows = await _rows(ch, org)
    await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["leftout_row"]["id"],
        peer_ids=[rows["leftout_flag"]["id"]],
        namespace=NS,
    )
    await worker.run_pass()
    # One judgement: the answer leaves a peer out, so it is asked once more with room.
    assert calls.count("leftout_row") == 2, calls


async def test_a_clickhouse_error_before_the_call_never_stamps_the_row(
    ch, wired, monkeypatch
) -> None:
    """The cluster read fails three passes in a row, before any call: the row stays owed."""
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 1300)

    async def boom(*a: Any, **kw: Any) -> Any:
        raise DatabaseError("Code: 202. TOO_MANY_SIMULTANEOUS_QUERIES")

    with monkeypatch.context() as m:
        m.setattr(queue, "cluster_peers", boom)
        for _ in range(3):
            await run_pass()
    assert llm.calls == [] and (await _marks(ch, org))["office_location"] == 1


async def test_the_resolver_pays_nothing_once_the_requests_spent_the_cap(ch, wired) -> None:
    """Requests spent the whole daily cap: the resolver's own share is untouched, but the
    namespace is over its cap, and the resolver makes no call."""
    from datetime import UTC, datetime

    from server.memory.workers.conflict_resolver import run_pass
    from server.observability.budget import namespace_over_cap

    org, llm = wired
    now = datetime.now(UTC)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": NS,
                "created_at": now,
                "updated_at": now,
                "active": True,
                "daily_cost_cap_usd": 0.40,
            }
        ],
    )
    await ch.insert(
        "loom.agent_events",
        [
            {
                "org": org,
                "namespace": NS,
                "ts": now,
                "operation": "memory.ask",
                "upstream_cost_usd": 0.50,
            }
        ],
    )
    assert await namespace_over_cap(org, NS)
    await _pair(ch, org, "office_city", "office_location", 1200)
    out = await run_pass()
    assert llm.calls == [] and out["capped_namespaces"] == 1, out


async def test_a_flag_no_cluster_can_hold_does_not_keep_the_row_owed(
    ch, wired, monkeypatch
) -> None:
    """The row is flagged (by cosine, namespace wide) against another user's row, which
    never joins its cluster. With a full cluster the row is still stamped after one
    judgement: no later pass could judge that flag."""
    from types import SimpleNamespace

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers import conflict_resolver as worker
    from server.settings import settings

    org, llm = wired
    monkeypatch.setattr(settings, "memory_conflict_resolver_max_peers", 2)
    calls: list[str] = []

    async def complete(system: str, user: str, **kw: Any) -> Any:
        calls.append(re.search(r"NEW note \(key ([^,)]+)", user).group(1))  # type: ignore[union-attr]
        entries = [
            {
                "peer_id": pid,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": "related",
                "confidence": 0.5,
            }
            for pid in re.findall(r"^\[(p\d+)\]", user, re.M)
        ]
        return SimpleNamespace(text=json.dumps({"peers": entries}))

    monkeypatch.setattr(llm, "complete", complete)

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "other_user_row", "The office is in Paris", user="u2")
        await _set(org, "s1", "near_a", "The office has a red door")
        await _set(org, "s1", "near_b", "The office has a blue roof")
        await _later()
        await _set(org, "s2", "judged_row", "The office moved to Rome")
        await after_write.drain()
    await _place(ch, org, "s1", "other_user_row", _vec((900, 1.0)), owed=False, user="u2")
    await _place(ch, org, "s1", "near_a", _vec((900, 0.9), (901, 0.43)), owed=False)
    await _place(ch, org, "s1", "near_b", _vec((900, 0.9), (902, 0.43)), owed=False)
    await _place(ch, org, "s2", "judged_row", _vec((900, 0.8), (903, 0.6)), owed=True)
    rows = await _rows(ch, org)
    landed = await conflicts.mark_peers_conflicted(
        org=org,
        new_memory_id=rows["judged_row"]["id"],
        peer_ids=[rows["other_user_row"]["id"]],
        namespace=NS,
    )
    assert landed == "conflict"
    for _ in range(3):
        await worker.run_pass()
    assert calls.count("judged_row") == 1, calls


async def test_a_row_owed_past_the_max_age_is_no_longer_judged(ch, wired, monkeypatch) -> None:
    """An owed row 31 days old is left out of the owed read and stays as it was written;
    one written today is judged."""
    from datetime import timedelta

    from server.memory.store import conflict_resolver as queue
    from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows
    from server.settings import settings

    org, _ = wired
    monkeypatch.setattr(settings, "memory_conflict_resolver_max_owed_age_days", 30)
    await _pair(ch, org, "office_city", "office_location", 280)
    await _pair(ch, org, "lead_old", "lead_new", 284)
    (stale,) = await live_embedding_rows_for_key(
        ch, org=org, namespace=NS, agent="bot", user_id=USER, session_id="s2", memory_key="lead_new"
    )
    await reemit_rows(
        [stale],
        {
            "created_at": stale["created_at"] - timedelta(days=31),
            "conflict_checked_at": queue.OWED_MARK,
        },
        ch=ch,
    )
    owed = {r["memory_key"] for r in await queue.owed_rows(limit=10, max_age_days=30)}
    assert owed == {"office_location"}, owed


async def test_the_lease_store_down_while_acting_stops_the_pass_and_keeps_the_row_owed(
    ch, wired, monkeypatch
) -> None:
    """The judge answers, then the keys' lease store stops answering: the pass stops and
    the row stays owed, not paid for every pass and then stamped with nothing done."""
    from contextlib import asynccontextmanager

    from server.core.leases import LeaseUnavailable
    from server.memory.store import key_write_lock
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    await _pair(ch, org, "office_city", "office_location", 1700)

    @asynccontextmanager
    async def down(*a: Any, **kw: Any) -> Any:
        raise LeaseUnavailable("keeper down", operation="memory.set")
        yield

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", down)
    for _ in range(3):
        await run_pass()
    assert (await _rows(ch, org))["office_city"]["status"] == "active"
    assert (await _marks(ch, org))["office_location"] == 1, llm.calls


@pytest.mark.parametrize("winner_valid_at", [False, True])
async def test_an_undo_of_a_value_update_retirement_holds(ch, wired, winner_valid_at: bool) -> None:
    """A value update (bulk_set's detect_updates) retires the older row with the
    winner's valid_at as its end, or none. After a person's Undo the resolver does not
    retire it again."""
    from datetime import UTC, datetime, timedelta

    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore
    from server.memory.app.value_updates import _supersession_update
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1720)
    rows = await _rows(ch, org)
    winner, loser = rows["office_location"]["id"], rows["office_city"]["id"]
    ended = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2) if winner_valid_at else None
    await memory_repo.write_versions_by_ids(
        org, [loser], _supersession_update(winner, ended), ch=ch, strong=True, namespace=NS
    )
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    undone = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            if_superseded_by=winner,
        )
    )
    assert undone.restored
    out = await run_pass()
    assert out["retired"] == 0, out
    assert (await _rows(ch, org))["office_city"]["status"] == "active"


async def test_a_writer_restating_a_retired_value_is_a_new_statement(
    ch, wired, monkeypatch
) -> None:
    """Rome retires Paris; Paris's writer then sets Paris again. That is no Undo but a
    later statement: judged as one, it retires Rome."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1740)
    assert (await run_pass())["retired"] == 1
    await _later()

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await after_write.drain()
    assert (await _rows(ch, org))["office_city"]["status"] == "active"
    await _place(ch, org, "s1", "office_city", _vec((1740, 1.0)), owed=True)
    monkeypatch.setitem(_SCRIPT, "office_city", ("update", 0.98))
    out = await run_pass()
    rows = await _rows(ch, org)
    assert out["retired"] == 1, out
    assert rows["office_location"]["superseded_by"] == rows["office_city"]["id"]


async def _undo(org: str, key: str, winner: str) -> Any:
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    return await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=key,
            if_superseded_by=winner,
        )
    )


async def test_a_pass_right_after_an_undo_sees_it(ch, wired, monkeypatch) -> None:
    """A pass landing once the Undo's lock is released, before its fact step ends,
    already finds the Undo's record: the winner judged again leaves both served."""
    from server.memory.app import restore
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1880)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]
    real = restore._reopen_facts
    seen: dict[str, Any] = {}

    async def reopen_then_pass(*a: Any, **kw: Any) -> None:
        await real(*a, **kw)
        await _place(ch, org, "s2", "office_location", _vec((1880, 0.8), (1881, 0.6)), owed=True)
        seen["out"] = await run_pass()

    monkeypatch.setattr(restore, "_reopen_facts", reopen_then_pass)
    assert (await _undo(org, "office_city", winner)).restored
    assert seen["out"]["retired"] == seen["out"]["flagged"] == 0, seen
    assert {r["status"] for r in (await _rows(ch, org)).values()} == {"active"}


async def test_an_undo_whose_record_fails_restores_nothing(ch, wired, monkeypatch) -> None:
    """The resolver honours an Undo by its record alone, so an Undo whose record is not
    written answers 503 and leaves the retirement as it was."""
    from server.core.errors import BackendUnavailableError
    from server.memory.workers.conflict_resolver import run_pass
    from server.observability import audit

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1820)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]

    async def lost(*a: Any, **kw: Any) -> str:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(audit, "emit_strict", lost)
    with pytest.raises(BackendUnavailableError):
        await _undo(org, "office_city", winner)
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"


async def test_an_undone_row_keeps_its_first_assertion(ch, wired) -> None:
    """An Undo writes the row's value back; the value was asserted when first written."""
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1800)
    assert (await run_pass())["retired"] == 1
    rows = await _rows(ch, org)
    loser, winner = rows["office_city"], rows["office_location"]
    assert (await _undo(org, "office_city", winner["id"])).restored
    history = await ch.query_all(
        "SELECT toString(content_sha256) AS h, version FROM loom.memory_versions"
        " WHERE org = {o:String} AND memory_id = {m:UUID} ORDER BY version"
        " SETTINGS select_sequential_consistency = 1",
        params={"o": org, "m": loser["id"]},
    )
    found = await queue.asserted_versions(org, NS, [(loser["id"], history[-1]["h"])], ch=ch)
    got = found.asserted
    assert got[loser["id"]] == history[0]["version"]


async def test_a_newer_statement_retires_an_undone_older_one(ch, wired, monkeypatch) -> None:
    """Paris retired by Rome, Berlin written, then Paris undone: judged against Berlin,
    Paris is still the older statement, and Berlin retires it."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1840)
    assert (await run_pass())["retired"] == 1
    await _later()

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s3", "office_site", "The office is in Berlin")
        await after_write.drain()
    await _place(ch, org, "s3", "office_site", _vec((1840, 0.9), (1842, 0.436)), owed=False)
    await _later()
    winner = (await _rows(ch, org))["office_location"]["id"]
    assert (await _undo(org, "office_city", winner)).restored
    await _place(ch, org, "s3", "office_site", _vec((1840, 0.9), (1842, 0.436)), owed=True)
    monkeypatch.setitem(_SCRIPT, "office_site", ("update", 0.98))
    await run_pass()
    rows = await _rows(ch, org)
    assert rows["office_site"]["status"] == "active"
    assert rows["office_city"]["superseded_by"] == rows["office_site"]["id"]


async def test_an_undo_whose_restore_failed_does_not_hold_a_later_restatement(
    ch, wired, monkeypatch
) -> None:
    """The Undo's record lands but its restore does not (503, nothing restored). The
    writer then states Paris again: a statement after the Undo, judged as new, so it
    retires Rome."""
    from server.core.errors import BackendUnavailableError
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 1960)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]

    async def lost(*a: Any, **kw: Any) -> None:
        raise RuntimeError("insert failed")

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lost)
        with pytest.raises(BackendUnavailableError, match="may not have landed"):
            await _undo(org, "office_city", winner)
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    await _later()
    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", "office_city", "The office is in Paris")
        await after_write.drain()
    await _place(ch, org, "s1", "office_city", _vec((1960, 1.0)), owed=True)
    monkeypatch.setitem(_SCRIPT, "office_city", ("update", 0.98))
    out = await run_pass()
    rows = await _rows(ch, org)
    assert out["retired"] == 1, out
    assert rows["office_location"]["superseded_by"] == rows["office_city"]["id"]


async def test_an_attest_of_a_retired_row_keeps_its_first_assertion(ch, wired, monkeypatch) -> None:
    """Paris retired by Rome, Berlin written; a person attests the retired Paris row (a
    copy, still retired), then undoes it. Paris was stated before Berlin, and Berlin's
    update retires it."""
    from server.memory.api.models_crud import MemoryVerifyInput
    from server.memory.app import after_write, conflicts
    from server.memory.app.verify import handle_memory_verify
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2140)
    assert (await run_pass())["retired"] == 1
    await _later()

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s3", "office_site", "The office is in Berlin")
        await after_write.drain()
    await _place(ch, org, "s3", "office_site", _vec((2140, 0.9), (2142, 0.436)), owed=False)
    await _later()
    await handle_memory_verify(
        MemoryVerifyInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
            actor="human:alice",
        )
    )
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    winner = (await _rows(ch, org))["office_location"]["id"]
    assert (await _undo(org, "office_city", winner)).restored
    await _place(ch, org, "s3", "office_site", _vec((2140, 0.9), (2142, 0.436)), owed=True)
    monkeypatch.setitem(_SCRIPT, "office_site", ("update", 0.98))
    out = await run_pass()
    rows = await _rows(ch, org)
    assert rows["office_city"]["superseded_by"] == rows["office_site"]["id"], out


async def test_an_undo_whose_insert_answer_was_lost_finishes(ch, wired, monkeypatch) -> None:
    """The restore's insert lands but its answer is lost: the re-read finds the restore,
    so the Undo goes on (search row, facts) and answers restored."""
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2101)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]
    real = lifecycle.insert_memory_versions

    async def lost(rows: Any, **kw: Any) -> None:
        await real(rows, **kw)
        raise TimeoutError("answer lost")

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lost)
        out = await _undo(org, "office_city", winner)
    assert out.restored
    assert (await _rows(ch, org))["office_city"]["status"] == "active"
    search = await ch.query_all(
        "SELECT status FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND memory_key = 'office_city' SETTINGS select_sequential_consistency = 1",
        params={"o": org},
    )
    assert search and {r["status"] for r in search} == {"active"}


async def test_memory_statuses_says_which_retirements_still_stand(ch, wired) -> None:
    """The Review queue reads each retired row's state now: a retirement whose Undo
    record outlived a failed restore still reads as retired behind its winner."""
    from server.memory.api.models_crud import (
        MemoryDeleteInput,
        MemoryRestoreInput,
        MemoryStatusesInput,
    )
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.query import handle_memory_statuses
    from server.memory.app.restore import handle_memory_restore
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2160)
    assert (await run_pass())["retired"] == 1
    rows = await _rows(ch, org)
    loser, winner = rows["office_city"]["id"], rows["office_location"]["id"]

    async def statuses() -> dict[str, tuple[str, str]]:
        out = await handle_memory_statuses(
            MemoryStatusesInput(org=org, namespace=NS, memory_ids=[loser, winner, "not-an-id"])
        )
        return {r.memory_id: (r.status, r.superseded_by) for r in out.rows}

    assert await statuses() == {loser: ("superseded", winner), winner: ("active", "")}
    # The most a call takes: more ids than one query parameter carries.
    many = [str(uuid.uuid4()) for _ in range(4998)] + [loser, winner]
    out = await handle_memory_statuses(MemoryStatusesInput(org=org, namespace=NS, memory_ids=many))
    assert {r.memory_id for r in out.rows} == {loser, winner}
    # An id asked twice, in either spelling, is answered once.
    twice = await handle_memory_statuses(
        MemoryStatusesInput(org=org, namespace=NS, memory_ids=[loser, loser.upper(), winner])
    )
    assert sorted(r.memory_id for r in twice.rows) == sorted([loser, winner])
    assert (await _undo(org, "office_city", winner)).restored
    assert (await statuses())[loser] == ("active", "")
    other = await handle_memory_statuses(
        MemoryStatusesInput(org=org, namespace="elsewhere", memory_ids=[loser])
    )
    assert other.rows == [], "another namespace holds none of them"
    # A delete, then its restore: the row reads deleted, then live again.
    key = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s2"}
    await handle_memory_delete(MemoryDeleteInput(**key, memory_key="office_location"))
    assert (await statuses())[winner][0] == "deleted"
    assert (
        await handle_memory_restore(MemoryRestoreInput(**key, memory_key="office_location"))
    ).restored
    assert (await statuses())[winner] == ("active", "")


async def test_a_retried_undo_finishes_a_restore_that_landed_after_its_503(
    ch, wired, monkeypatch
) -> None:
    """The restore's insert lands only after the re-read, so the Undo answered 503. The
    person's retry finds the row served at the version its record names: it finishes
    that Undo (the search row served again) and answers restored."""
    from server.core.errors import BackendUnavailableError
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2190)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]
    real = lifecycle.insert_memory_versions
    late: list[Any] = []

    async def lost(rows: Any, **kw: Any) -> None:
        late.append((rows, kw))
        raise TimeoutError("answer lost")

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lost)
        with pytest.raises(BackendUnavailableError):
            await _undo(org, "office_city", winner)
    rows, kw = late[0]
    await real(rows, **kw)  # it lands now
    assert (await _undo(org, "office_city", winner)).restored
    search = await ch.query_all(
        "SELECT status FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND memory_key = 'office_city' SETTINGS select_sequential_consistency = 1",
        params={"o": org},
    )
    assert search and {r["status"] for r in search} == {"active"}


async def _versions_of(ch: Any, org: str, key: str) -> int:
    """Distinct versions: the ReplacingMergeTree keeps a repeat of one until a merge."""
    rows = await ch.query_all(
        "SELECT uniqExact(version) AS n FROM loom.memory_versions WHERE org = {o:String}"
        " AND memory_key = {k:String} SETTINGS select_sequential_consistency = 1",
        params={"o": org, "k": key},
    )
    return int(rows[0]["n"])


async def test_an_undo_naming_another_memory_after_a_restore_changes_nothing(ch, wired) -> None:
    """A row an Undo restored, undone again behind a memory it never retired behind:
    no Undo of that names this restore, so nothing is written and nothing restored."""
    import uuid

    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2195)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]
    assert (await _undo(org, "office_city", winner)).restored
    before = await _versions_of(ch, org, "office_city")
    assert not (await _undo(org, "office_city", str(uuid.uuid4()))).restored
    assert (await _undo(org, "office_city", winner)).restored, "the same Undo again"
    assert await _versions_of(ch, org, "office_city") == before


async def _late_undo(org: str, winner: str, monkeypatch: Any) -> tuple[Any, dict[str, Any]]:
    """An Undo whose insert is held back (its answer lost): the rows and kwargs to land."""
    from server.core.errors import BackendUnavailableError

    held: list[Any] = []

    async def lost(rows: Any, **kw: Any) -> None:
        held.append((rows, kw))
        raise TimeoutError("answer lost")

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lost)
        with pytest.raises(BackendUnavailableError):
            await _undo(org, "office_city", winner)
    return held[0]


async def _search_status(ch: Any, org: str, key: str) -> set[str]:
    rows = await ch.query_all(
        "SELECT status FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND memory_key = {k:String} AND tombstoned_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"o": org, "k": key},
    )
    return {r["status"] for r in rows}


async def test_two_lost_undos_landing_out_of_order_finish_on_retry(ch, wired, monkeypatch) -> None:
    """Two Undo attempts both answered 503; the later one's insert lands first. The head
    is the later restore, which its own record names: the retry finishes it."""
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2196)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]
    first = await _late_undo(org, winner, monkeypatch)
    second = await _late_undo(org, winner, monkeypatch)
    for rows, kw in (second, first):
        await lifecycle.insert_memory_versions(rows, **kw)
    out = await _undo(org, "office_city", winner)
    assert out.restored and out.version == second[0][0]["version"]
    assert await _search_status(ch, org, "office_city") == {"active"}


async def test_an_unguarded_retry_finishes_an_undo_that_landed_late(ch, wired, monkeypatch) -> None:
    """The retry names no memory: the head is an Undo's restore all the same, so it is
    finished (search row served, facts reopened) and answers restored."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2197)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]
    rows, kw = await _late_undo(org, winner, monkeypatch)
    await lifecycle.insert_memory_versions(rows, **kw)
    reopened: list[str] = []
    real = restore._reopen_facts

    async def spy(input: Any, existing: Any, *a: Any, **k: Any) -> None:
        reopened.append(str(existing.memory_id))
        await real(input, existing, *a, **k)

    monkeypatch.setattr(restore, "_reopen_facts", spy)
    out = await restore.handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="office_city",
        )
    )
    assert out.restored
    assert await _search_status(ch, org, "office_city") == {"active"}
    assert reopened == [str(rows[0]["memory_id"])]


async def test_reconcile_status_clears_the_end_of_an_undone_rows_search_row(
    ch, wired, monkeypatch
) -> None:
    """An Undo whose search-row write failed leaves that row retired, with its end;
    `reconcile-status` serves it again with the restore's open end, not the old one."""
    import server.memory.store as memory_repo
    from server.memory.store.embeddings import reconcile_embedding_status
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2198)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]

    async def boom(*a: Any, **kw: Any) -> int:
        raise TimeoutError("search row write failed")

    with monkeypatch.context() as m:
        m.setattr(memory_repo, "unsupersede_embeddings", boom)
        assert (await _undo(org, "office_city", winner)).restored
    await reconcile_embedding_status(org, NS, dry_run=False, ch=ch)
    search = await ch.query_all(
        "SELECT status, invalid_at FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND memory_key = 'office_city' AND tombstoned_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"o": org},
    )
    assert [(r["status"], r["invalid_at"]) for r in search] == [("active", None)]


async def test_a_repeat_undo_writes_no_new_facts_or_search_rows(ch, wired, monkeypatch) -> None:
    """Rome retires Paris by a value update and a person undoes it; the same Undo again
    (the queue offers it when it cannot read the row) finishes the same restore: no new
    fact rows, no new search-row versions."""
    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult

    org, _ = wired

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(
            org,
            "s1",
            "office_city",
            "The office is in Paris",
            relations=[("office", "located_in", "paris")],
        )
        await _later()
        await _set(
            org,
            "s2",
            "office_location",
            "The office moved to Rome",
            relations=[("office", "located_in", "rome")],
        )
        await after_write.drain()
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"
    winner = (await _rows(ch, org))["office_location"]["id"]

    async def counts() -> tuple[int, int, list[Any]]:
        await after_write.drain()
        facts = await ch.query_all(
            "SELECT count() AS n FROM loom.entity_triples WHERE org = {o:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"o": org},
        )
        # The newest search-row version and what its rows say, not a row count: a merge
        # of the ReplacingMergeTree drops replaced versions between two counts, and a
        # re-emit that kept the version shows in the rows at it.
        search = await ch.query_all(
            "SELECT version AS v, arraySort(groupUniqArray(tuple("
            "created_at, status, conflict_checked_at, content_sha256))) AS rows"
            " FROM loom.memory_embeddings WHERE org = {o:String} AND memory_key = 'office_city'"
            " GROUP BY version ORDER BY version DESC LIMIT 1"
            " SETTINGS select_sequential_consistency = 1",
            params={"o": org},
        )
        return int(facts[0]["n"]), int(search[0]["v"]), list(search[0]["rows"])

    assert (await _undo(org, "office_city", winner)).restored
    first = await counts()
    for _ in range(2):
        assert (await _undo(org, "office_city", winner)).restored
        assert await counts() == first


async def test_a_plain_restore_of_a_live_row_takes_no_lock(ch, wired, monkeypatch) -> None:
    """A row never retired nor deleted: memory.restore is a no-op, without the key's
    write lock (nor an Undo's wording when Keeper is down)."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore

    org, _ = wired
    await _set(org, "s1", "plain_key", "A plain row")
    taken: list[str] = []
    real = restore._key_lock

    def spy(input: Any, what: str) -> Any:
        taken.append(input.memory_key)
        return real(input, what)

    monkeypatch.setattr(restore, "_key_lock", spy)
    out = await restore.handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="plain_key",
        )
    )
    assert not out.restored and taken == []


class _Garbage:
    """A model whose every answer is unusable."""

    provider, model = "fake", "garbage"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(text="I cannot answer in JSON today.")


async def test_a_refused_request_every_pass_stamps_nothing(ch, wired, monkeypatch) -> None:
    """The provider refuses every request (a 400, not billed), pass after pass: the
    request is at fault, not the rows, so no row counts an attempt or is stamped."""
    import httpx

    import server.llm.client as llm_client
    from server.memory.workers import conflict_resolver as worker

    org, _ = wired
    await _seed(ch, org, monkeypatch)

    class _Refuses:
        provider, model = "fake", "refuses"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            self.calls += 1
            request = httpx.Request("POST", "https://provider.invalid/v1")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    refuses = _Refuses()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: refuses)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_concurrency", 1)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_pause_after_stop_seconds", 0.0)
    for _ in range(5):
        await worker.run_pass()
    assert refuses.calls > 0
    marks = await _marks(ch, org)
    assert marks["office_location"] == 1 and marks["budget_next"] == 1, marks


async def test_an_unusable_answer_every_pass_ends_after_three_attempts(
    ch, wired, monkeypatch
) -> None:
    """Answers with nothing usable were paid for: each counts an attempt, so after three
    a row is stamped and the passes come to pay nothing."""
    import server.llm.client as llm_client
    from server.memory.workers import conflict_resolver as worker

    org, _ = wired
    await _seed(ch, org, monkeypatch)
    garbage = _Garbage()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: garbage)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_concurrency", 1)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_pause_after_stop_seconds", 0.0)
    per_pass = []
    for _ in range(8):
        before = garbage.calls
        await worker.run_pass()
        per_pass.append(garbage.calls - before)
    assert per_pass[0] > 0 and per_pass[-2:] == [0, 0], per_pass
    assert (await _marks(ch, org))["office_location"] != 1, "stamped"


async def test_one_namespaces_refusals_do_not_pause_another(ch, wired, monkeypatch) -> None:
    """Refusals row after row in one namespace pause that namespace; the other's rows
    are judged in the same pass and the next."""
    import server.llm.client as llm_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, conflicts
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.store.conflict_resolver import OWED_MARK
    from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows
    from server.memory.workers import conflict_resolver as worker

    org, llm = wired
    await _seed(ch, org, monkeypatch)
    other = "resolver-b"

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    with monkeypatch.context() as m:
        m.setattr(conflicts, "check_on_commit", clear)
        for session, key, value in (
            ("s1", "team_size", "The team has five people"),
            ("s2", "team_size_now", "The team has six people"),
        ):
            await handle_memory_set(
                MemorySetInput(
                    org=org,
                    namespace=other,
                    agent="bot",
                    user_id=USER,
                    session_id=session,
                    memory_key=key,
                    value=value,
                )
            )
            if session == "s1":
                await _later()
        await after_write.drain()
    for session, key, vec, owed in (
        ("s1", "team_size", _vec((40, 1.0)), False),
        ("s2", "team_size_now", _vec((40, 0.8), (41, 0.6)), True),
    ):
        rows = await live_embedding_rows_for_key(
            ch,
            org=org,
            namespace=other,
            agent="bot",
            user_id=USER,
            session_id=session,
            memory_key=key,
        )
        await reemit_rows(
            rows, {"embedding": vec, "conflict_checked_at": OWED_MARK if owed else None}, ch=ch
        )
    refused_keys = {"office_location", "budget_next", "deploy_day_now", "standup_lead", "lead_name"}

    class _Mixed:
        provider, model = "fake", "mixed"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            import re
            from types import SimpleNamespace

            key = re.search(r"NEW note \(key ([^,)]+)", user).group(1)  # type: ignore[union-attr]
            self.calls.append(key)
            if key in refused_keys:
                return SimpleNamespace(text="I cannot answer in JSON today.")
            return await llm.complete(system, user, **kw)

    mixed = _Mixed()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: mixed)
    monkeypatch.setitem(_SCRIPT, "team_size_now", ("update", 0.98))
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_concurrency", 1)
    out = await worker.run_pass()
    assert out["retired"] == 1, (out, mixed.calls)
    assert worker._state.paused_until == 0.0, "nobody else waits"
    assert (org, NS) in worker._state.paused_namespaces, mixed.calls
    before = len(mixed.calls)
    await worker.run_pass()
    assert not set(mixed.calls[before:]) & refused_keys, "the refusing namespace waits"


async def test_a_pass_stopped_by_the_lease_store_pauses_the_worker(ch, wired, monkeypatch) -> None:
    """Keeper down: the first pass pays for a call whose settle cannot take a lock and
    stops; the passes after it pay nothing until the pause ends."""
    from server.core import leases
    from server.memory.workers import conflict_resolver as worker

    org, llm = wired
    await _seed(ch, org, monkeypatch)

    async def down(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("keeper unreachable")

    monkeypatch.setattr(leases, "take", down)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_concurrency", 1)
    per_pass = []
    for _ in range(3):
        before = len(llm.calls)
        await worker.run_pass()
        per_pass.append(len(llm.calls) - before)
    assert per_pass[0] >= 1 and per_pass[1:] == [0, 0], per_pass
    monkeypatch.setattr(worker._state, "paused_until", 0.0)
    before = len(llm.calls)
    await worker.run_pass()
    assert len(llm.calls) > before, "the pause ended: rows are judged again"


async def test_an_undelete_whose_insert_answer_was_lost_finishes(ch, wired, monkeypatch) -> None:
    """The undelete's insert lands with its answer lost: the re-read finds it, so the
    search row and the facts come back as on any undelete, and it answers restored."""
    import asyncio

    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput
    from server.memory.app import restore
    from server.memory.app.handlers import handle_memory_delete

    org, _ = wired
    key = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    await _set(org, "s1", "home2", "Ana lives in Porto", relations=[("ana", "lives_in", "porto")])
    await asyncio.sleep(1.0)
    await handle_memory_delete(MemoryDeleteInput(**key, memory_key="home2"))
    await asyncio.sleep(1.0)
    real = lifecycle.insert_memory_versions

    async def lands_then_fails(rows: Any, **kw: Any) -> None:
        await real(rows, **kw)
        raise RuntimeError("answer lost")

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lands_then_fails)
        out = await restore.handle_memory_restore(MemoryRestoreInput(**key, memory_key="home2"))
    assert out.restored and out.from_status == "tombstoned"
    search = await ch.query_all(
        "SELECT tombstoned_at FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND memory_key = 'home2' SETTINGS select_sequential_consistency = 1",
        params={"o": org},
    )
    assert search and all(r["tombstoned_at"] is None for r in search)


async def test_a_plain_restore_whose_undo_record_read_fails_is_a_no_op(
    ch, wired, monkeypatch
) -> None:
    """The Undo-record read fails: the live row is answered as any other, not an error."""
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore
    from server.memory.store import conflict_resolver as queue

    org, _ = wired
    await _set(org, "s1", "plain_key", "A plain row")

    async def down(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("audit_log unreachable")

    monkeypatch.setattr(queue, "undo_recorded", down)
    out = await restore.handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="plain_key",
        )
    )
    assert not out.restored and out.memory_id


async def test_an_undo_retry_whose_record_read_fails_answers_503(ch, wired, monkeypatch) -> None:
    """The retry of an Undo whose restore landed with its answer lost cannot read the
    record: it answers 503, outcome unknown, not "nothing to undo"."""
    from server.core.errors import BackendUnavailableError
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 2199)
    assert (await run_pass())["retired"] == 1
    winner = (await _rows(ch, org))["office_location"]["id"]

    rows, kw = await _late_undo(org, winner, monkeypatch)
    await lifecycle.insert_memory_versions(rows, **kw)

    async def down(*a: Any, **k: Any) -> bool:
        raise RuntimeError("audit_log unreachable")

    monkeypatch.setattr(queue, "undo_recorded", down)
    with pytest.raises(BackendUnavailableError, match="could not tell"):
        await _undo(org, "office_city", winner)


async def test_a_provider_outage_stops_the_pass_and_pauses_the_worker(
    ch, wired, monkeypatch
) -> None:
    """No connection to the provider, row after row: the pass stops after the third,
    the worker pauses, and no row counts an attempt."""
    import httpx

    import server.llm.client as llm_client
    from server.memory.workers import conflict_resolver as worker

    org, _ = wired
    await _seed(ch, org, monkeypatch)

    class _Down:
        provider, model = "fake", "down"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            self.calls += 1
            raise httpx.ConnectError("provider unreachable")

    down = _Down()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: down)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_concurrency", 1)
    await worker.run_pass()
    assert 0 < down.calls <= worker.SAME_REFUSAL_STOP
    assert worker._state.paused_until > 0 and worker._state.attempts == {}
    before = down.calls
    await worker.run_pass()
    assert down.calls == before, "paused"


async def test_an_undelete_whose_answer_and_reread_were_lost_finishes_on_retry(
    ch, wired, monkeypatch
) -> None:
    """The undelete's insert lands with its answer lost and the re-read fails too (503):
    the retry finds the row live but its search row deleted, and brings that back."""
    import asyncio

    from server.core.errors import BackendUnavailableError
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput
    from server.memory.app import restore
    from server.memory.app.handlers import handle_memory_delete

    org, _ = wired
    key = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    await _set(org, "s1", "home3", "Ana lives in Braga")
    await asyncio.sleep(1.0)
    await handle_memory_delete(MemoryDeleteInput(**key, memory_key="home3"))
    await asyncio.sleep(1.0)
    real = lifecycle.insert_memory_versions

    async def lands_then_fails(rows: Any, **kw: Any) -> None:
        await real(rows, **kw)
        raise RuntimeError("answer lost")

    async def unread(*a: Any, **kw: Any) -> None:
        return None  # the re-read fails too: ClickHouse never settles a miss

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lands_then_fails)
        m.setattr(restore, "_restore_landed", unread)
        with pytest.raises(BackendUnavailableError) as lost:
            await restore.handle_memory_restore(MemoryRestoreInput(**key, memory_key="home3"))
        assert lost.value.extra["outcome_unknown"]
    out = await restore.handle_memory_restore(MemoryRestoreInput(**key, memory_key="home3"))
    assert out.restored and out.from_status == "tombstoned"
    search = await ch.query_all(
        "SELECT tombstoned_at FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND memory_key = 'home3' SETTINGS select_sequential_consistency = 1",
        params={"o": org},
    )
    assert search and all(r["tombstoned_at"] is None for r in search)
    again = await restore.handle_memory_restore(MemoryRestoreInput(**key, memory_key="home3"))
    assert not again.restored, "finished: a plain no-op now"


async def test_a_flapping_provider_stops_the_pass(ch, wired, monkeypatch) -> None:
    """No connection, then a 503, then a timeout: transient failures of any kind, row
    after row, are an outage, and stop the pass."""
    import httpx

    import server.llm.client as llm_client
    from server.memory.workers import conflict_resolver as worker

    org, _ = wired
    await _seed(ch, org, monkeypatch)

    class _Flapping:
        provider, model = "fake", "flapping"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            self.calls += 1
            request = httpx.Request("POST", "https://provider.invalid/v1")
            if self.calls % 3 == 1:
                raise httpx.ConnectError("unreachable", request=request)
            if self.calls % 3 == 2:
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError("503", request=request, response=response)
            raise TimeoutError("read timed out")

    flapping = _Flapping()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: flapping)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_concurrency", 1)
    await worker.run_pass()
    assert flapping.calls == worker.SAME_REFUSAL_STOP
    assert worker._state.paused_until > 0


async def test_a_high_row_restored_after_a_lost_undelete_answers_not_restored(
    ch, wired, monkeypatch
) -> None:
    """A high row's undelete lost its answer and its re-read: the retry has no search row
    to bring back, so it answers what it did, nothing."""
    import asyncio

    from server.core.errors import BackendUnavailableError
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput
    from server.memory.app import restore
    from server.memory.app.handlers import handle_memory_delete

    org, _ = wired
    key = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    await _set(org, "s1", "vault4", "The vault code is 4417", sensitivity="high")
    await asyncio.sleep(1.0)
    await handle_memory_delete(MemoryDeleteInput(**key, memory_key="vault4"))
    await asyncio.sleep(1.0)
    real = lifecycle.insert_memory_versions

    async def lands_then_fails(rows: Any, **kw: Any) -> None:
        await real(rows, **kw)
        raise RuntimeError("answer lost")

    async def unread(*a: Any, **kw: Any) -> None:
        return None  # the re-read fails too: ClickHouse never settles a miss

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "insert_memory_versions", lands_then_fails)
        m.setattr(restore, "_restore_landed", unread)
        with pytest.raises(BackendUnavailableError) as lost:
            await restore.handle_memory_restore(MemoryRestoreInput(**key, memory_key="vault4"))
        assert lost.value.extra["outcome_unknown"]
    out = await restore.handle_memory_restore(MemoryRestoreInput(**key, memory_key="vault4"))
    assert not out.restored


@pytest.mark.parametrize("holder", ["judged row", "peer", "peer key"])
async def test_an_unlabelled_credential_never_reaches_the_judge(
    ch, wired, monkeypatch, holder
) -> None:
    """A value holding a credential shape that no write labelled 'high' (detection off),
    on the judged row or on its peer: the resolver reads it before its prompt and makes
    no model call; the judged row is stamped."""
    import uuid

    from server.memory.app import after_write, conflicts
    from server.memory.domain.conflicts import ConflictResult
    from server.memory.workers.conflict_resolver import run_pass
    from server.settings import settings

    org, llm = wired
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]

    async def clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    old = f"The deploy token is {secret}" if holder == "peer" else "The deploy token is unset"
    new = f"The deploy token is now {secret}" if holder == "judged row" else "The token rotated"
    # The prompt shows each note's key too.
    old_key = f"token/{secret}" if holder == "peer key" else "deploy_token"
    with monkeypatch.context() as m:
        m.setattr(settings, "memory_sensitivity_autodetect", False)
        m.setattr(conflicts, "check_on_commit", clear)
        await _set(org, "s1", old_key, old)
        await _later()
        await _set(org, "s2", "deploy_token_now", new)
        await after_write.drain()
    await _place(ch, org, "s1", old_key, _vec((40, 1.0)), owed=False)
    await _place(ch, org, "s2", "deploy_token_now", _vec((40, 0.8), (41, 0.6)), owed=True)

    out = await run_pass()
    assert llm.calls == [] and out["judged"] == 0
    assert (await _marks(ch, org))["deploy_token_now"] == 2, "stamped judged"


async def test_auto_retirements_reads_only_the_callers_users_records(ch) -> None:
    """Two users' retirements on record: a read narrowed to one user lists only that
    user's entries, a namespace-wide viewer lists both, newest record first."""
    from server.identity.domain.read_scope import (
        WHOLE_NAMESPACE,
        ReadFilters,
        bind_request_read_filters,
    )
    from server.memory.api.models_crud import MemoryAutoRetirementsInput
    from server.memory.app import auto_resolution as ar
    from tests.unit.principals import acting_as, principal

    org = unique_org("auto-retirements")
    for user in ("u1", "u2"):
        entry = {
            "memory_id": str(uuid.uuid4()),
            "memory_key": f"{user}_city",
            "agent": "bot",
            "user_id": user,
            "session_id": "s1",
            "superseded_by": str(uuid.uuid4()),
            "judge_confidence": "0.9",
        }
        assert await ar.record_auto_supersessions(
            org=org, namespace=NS, retired=[entry], reason=ar.CONFLICT_RESOLVER
        )
        await asyncio.sleep(0.01)  # the records' timestamps order them

    async def users(**kw: Any) -> list[str]:
        out = await ar.handle_memory_auto_retirements(
            MemoryAutoRetirementsInput(org=org, namespace=NS, **kw)
        )
        return [r.user_id for r in out.rows]

    assert await users() == ["u2", "u1"]
    auth = principal("viewer", org=org, namespaces=(NS,))
    try:
        with acting_as(auth):
            bind_request_read_filters(auth, ReadFilters("own_user", "u1", None))
            assert await users() == ["u1"]
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    # Paged one record at a time, the cursor walks both.
    first = await ar.handle_memory_auto_retirements(
        MemoryAutoRetirementsInput(org=org, namespace=NS, limit=1)
    )
    assert [r.user_id for r in first.rows] == ["u2"] and first.next_cursor
    assert await users(limit=1, cursor=first.next_cursor) == ["u1"]


async def _by_id(memory_id: str, **kw: Any) -> Any:
    """A row by id, in the one namespace that holds it."""
    import server.memory.store as M

    (namespace,) = await M.namespaces_of_id(kw["org"], memory_id, strong=False)
    return await M.get_by_id(memory_id, namespace=namespace, **kw)
