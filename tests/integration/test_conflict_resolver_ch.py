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
from typing import Any

import pytest

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

        key = re.search(r"NEW note \(key ([^)]+)\)", user).group(1)  # type: ignore[union-attr]
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
    from server.settings import settings

    org = unique_org("resolver")
    llm = _ScriptedLLM()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: llm)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)

    async def newest_wins(org: str, namespace: str, ch: Any = None) -> str:
        return "newest_wins"

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", newest_wins)
    # A test that needs a second org appends it to `orgs`.
    only = _OnlyOrgs(ch, [org])
    for name in ("queued_namespaces", "owed_rows", "unjudged_conflicted_rows"):
        read = getattr(queue, name)
        monkeypatch.setattr(queue, name, lambda read=read, **kw: read(**kw, ch=only))
    llm.orgs = only.orgs
    return org, llm


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
        org=org, new_memory_id=rows["standup_lead"]["id"], peer_ids=[rows["lead_name"]["id"]]
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
    assert out["retired"] == 1 and out["cleared"] == 1 and out["skipped_pinned"] == 1
    assert out["skipped_low_confidence"] >= 1 and out["errored"] == 0
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


async def test_a_write_with_a_peer_past_the_floor_is_owed_a_judgement(ch, wired) -> None:
    """The write path marks a row whose peer sits between the floor and the threshold;
    a row with no peer that near is not marked."""
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
            return _vec((10, 0.7), (11, 0.714)) if "cat" in text else _vec((12, 1.0))

    with pytest.MonkeyPatch.context() as m:
        m.setattr(set_commit, "get_embedder", lambda: Placed())
        await _set(org, "s2", "pet_new", "The user also has a cat named Tom")
        await _set(org, "s2", "car", "The user drives a blue car")
    marks = await _marks(ch, org)
    assert marks["pet_new"] == 1 and marks["car"] is None


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
    versions = await ch.query_all(
        "SELECT count() AS n FROM loom.memory_versions WHERE org = {org:String}"
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

    await run_pass()
    assert "office_location" in llm.calls
    assert (await _marks(ch, org))["office_location"] == 2


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
        if "NEW note (key x_location)" in user:
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


async def test_a_point_write_of_a_key_another_session_holds_is_owed(ch, wired, monkeypatch) -> None:
    """memory.set and memory.update_by_id from s2 of a key s1 holds live are owed a
    judgement under auto_reinforce, though no peer sits near them."""
    from server.embedding import get_embedder
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import after_write, conflicts, set_commit, update
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def auto(org: str, namespace: str, ch: Any = None) -> str:
        return "auto_reinforce"

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", auto)
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
    assert sum("unavailable" in r.message for r in caplog.records) == 2, "once per pass"
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
    row = await resolver._head(org, ids["office_location"])
    judged_peer = await resolver._head(org, ids["office_city"])
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
    await run_pass()
    assert "office_location" in llm.calls
    assert (await _rows(ch, org))["office_city"]["status"] == "superseded"


async def _follow_intents(ch: Any, org: str, want_active: int) -> list[dict[str, Any]]:
    # Resolving is a mutation: poll up to ten seconds for `want_active` active rows.
    rows: list[dict[str, Any]] = []
    for _ in range(50):
        rows = await ch.query_all(
            "SELECT status, payload FROM loom.memory_projection_failures"
            " WHERE org = {org:String} AND projection = 'triples_follow_owed'"
            " SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1",
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
    from server.memory.app import entity_projections
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 66)
    if follow_fails:

        async def boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("graph unavailable")

        monkeypatch.setattr(entity_projections, "follow_row_supersessions", boom)
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
        (retry,) = await retry_projection_failures(org=org, projection=FOLLOW_OWED)
        assert retry.resolved, retry


async def test_a_flagged_pair_is_judged_once_from_both_sides(ch, wired, monkeypatch) -> None:
    from server.memory.app import conflicts
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired

    async def auto(org: str, namespace: str, ch: Any = None) -> str:
        return "auto_reinforce"

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", auto)
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
        org=org, new_memory_id=rows["office_location"]["id"], peer_ids=[rows["office_city"]["id"]]
    )
    assert landed == "conflict"

    await run_pass()
    assert not any("Paris" in p for p in prompts)
    after = await _rows(ch, org)
    assert after["office_city"]["status"] == "conflicted", "u2's row is not retired"


async def test_a_retirement_waits_for_a_conditional_revision_of_the_peer(ch, wired) -> None:
    """A conditional revision of the peer holds its key from its read to its insert.
    The resolver does not retire the peer meanwhile; it then sees the revision and
    leaves the pair alone, so the revision is what FINAL serves."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.store.key_write_lock import key_write_locks
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 72)
    peer = (await _rows(ch, org))["office_city"]
    async with key_write_locks.hold(org, NS, "bot", USER, "s1", "office_city"):
        resolving = asyncio.create_task(run_pass())
        await asyncio.sleep(2)
        assert (await _rows(ch, org))["office_city"]["status"] == "active"
        # The revision's insert, inside the lock (the lock itself is held by the test).
        await handle_memory_update_by_id(
            MemoryUpdateByIdInput(org=org, memory_id=peer["id"], value="The office is in Lyon")
        )
    out = await resolving
    after = (await _rows(ch, org))["office_city"]
    assert after["status"] == "active" and after["version"] > peer["version"]
    assert out["retired"] == 0 and out["skipped_changed"] == 1


async def test_a_retirement_clears_the_winners_flag_against_the_retired_row(ch, wired) -> None:
    """The write path flagged the pair; the resolver retires the older row. The winner
    is active again, with no flag left, and the audit row says the flag went."""
    from server.memory.app import conflicts
    from server.memory.workers.conflict_resolver import run_pass

    org, _ = wired
    await _pair(ch, org, "office_city", "office_location", 74)
    rows = await _rows(ch, org)
    landed = await conflicts.mark_peers_conflicted(
        org=org, new_memory_id=rows["office_location"]["id"], peer_ids=[rows["office_city"]["id"]]
    )
    assert landed == "conflict"
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
