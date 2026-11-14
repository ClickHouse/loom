"""A definition is identified by its key: nothing judges it against a row under another
key, and its namespace-wide row is revised only by its own write."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import consolidate
from server.memory.domain.conflicts import intra_batch_supersessions
from server.memory.domain.memory_types import DEFINITION, is_namespace_definition
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


def test_only_the_namespace_wide_row_is_the_namespace_definition() -> None:
    row = {"memory_type": "definition", "agent": "-", "user_id": "-", "session_id": "-"}
    assert is_namespace_definition(row)
    assert not is_namespace_definition({**row, "session_id": "s1"})
    assert not is_namespace_definition({**row, "memory_type": "semantic"})


def test_two_definitions_in_one_batch_supersede_neither() -> None:
    values = ["ARR stands for annual recurring revenue", "ARR stands for annual run rate"]
    assert intra_batch_supersessions(values, ["semantic", "semantic"]) == [(0, 1)]
    assert intra_batch_supersessions(values, ["definition", "definition"]) == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_consolidation_reads_no_definition() -> None:
    def row(key: str, memory_type: str) -> str:
        return (
            "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
            f"memory_type, value, status, version, created_at) VALUES ('o', 'n', 'a', '-', '-', "
            f"'{key}', '{memory_type}', 'v', 'active', 1, now() - 86400)"
        )

    full = "arrayWithConstant(3072, 0.5)"  # the codec's width

    def vector(key: str) -> str:
        return (
            "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
            "memory_key, embedding_model, embedding_dim, embedding, memory_key_embedding) "
            f"VALUES ('o', 'n', 'a', '-', '-', '{key}', 'm', 3072, {full}, {full})"
        )

    setup = [
        ddl("memory"),
        "SET enable_quantized_codec = 1",  # the embeddings table's codec
        ddl("memory_embeddings"),
        row("definition/arr", "definition"),
        row("arr_note", "semantic"),
        vector("definition/arr"),
        vector("arr_note"),
    ]
    params = {
        "org": "o",
        "ns": "n",
        "agent": "a",
        "uid": "-",
        "limit": 10,
        "model": "m",
        "definition": DEFINITION,
        "age_cutoff": datetime.now(UTC).replace(tzinfo=None),
    }
    rows = run(setup, consolidate._CONSOLIDATION_CANDIDATES_SQL, params)
    assert [r["memory_key"] for r in rows] == ["arr_note"]


async def test_a_postgres_same_key_read_keeps_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import key_supersession as ks

    head = {
        "memory_id": "d1",
        "namespace": "n",
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "definition/arr",
        "memory_type": "definition",
        "status": "active",
    }

    async def live_key_heads(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [head]

    monkeypatch.setattr(ks.pg, "enabled", lambda: True)
    monkeypatch.setattr(ks.pg_memory, "live_key_heads", live_key_heads)
    monkeypatch.setattr(ks, "request_scope", lambda: None)
    rows = await ks._live_same_key_rows("o", "n", ["definition/arr"], ch=None)
    assert ks.same_key_victims([{"memory_id": "n1", "memory_key": "definition/arr"}], rows) == []


async def test_definitions_past_the_cap_leave_room_for_the_facts_behind_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set, conflicts
    from server.memory.domain.conflicts import ConflictResult

    checked: list[str] = []

    async def check_on_commit(*, memory_key: str, **kw: Any) -> ConflictResult:
        checked.append(memory_key)
        return ConflictResult(verdict="clear", peers=[])

    async def policy(*a: Any) -> str:
        return "append_only"  # no peer prefetch: only the cap is under test

    monkeypatch.setattr(conflicts, "check_on_commit", check_on_commit)
    monkeypatch.setattr(bulk_set, "_namespace_policy", policy)
    entries = [
        SimpleNamespace(memory_type="definition", memory_key=f"definition/t{i}") for i in range(3)
    ]
    entries.append(SimpleNamespace(memory_type="semantic", memory_key="arr_note"))
    metas = [(e, "v", None, None, None) for e in entries]
    pending = [(i, "text", "excerpt", "key text") for i in range(4)]
    out = await bulk_set._bulk_conflict_verdicts(
        SimpleNamespace(org="o", namespace="n", agent="a"),
        [{"memory_type": e.memory_type} for e in entries],
        metas,
        pending,
        [[0.6, 0.8]] * 4,
        floors={},
        cap=1,
    )
    # Every definition is clear without a check, and the cap's one place goes to the fact.
    assert checked == ["arr_note"] and sorted(out) == [0, 1, 2, 3]
    assert {out[i].verdict for i in range(3)} == {"clear"}
    # A batch of definitions alone answers each one clear.
    only = await bulk_set._bulk_conflict_verdicts(
        SimpleNamespace(org="o", namespace="n", agent="a"),
        [{"memory_type": e.memory_type} for e in entries[:3]],
        metas[:3],
        pending[:3],
        [[0.6, 0.8]] * 3,
        floors={},
        cap=1,
    )
    assert {i: v.verdict for i, v in only.items()} == {0: "clear", 1: "clear", 2: "clear"}
    # At cap 0 nothing is checked, and the batch answers as one with no definition would.
    out = await bulk_set._bulk_conflict_verdicts(
        SimpleNamespace(org="o", namespace="n", agent="a"),
        [{"memory_type": e.memory_type} for e in entries],
        metas,
        pending,
        [[0.6, 0.8]] * 4,
        floors={},
        cap=0,
    )
    assert out == {}


async def test_a_postgres_point_write_reads_no_peer_for_a_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import conflicts, set_commit

    async def distance(*a: Any, **kw: Any) -> float:
        raise AssertionError("a definition has no peer reach")

    async def peer_rows(**kw: Any) -> list[Any]:
        raise AssertionError("a definition reads no peer")

    monkeypatch.setattr(conflicts, "peer_distance", distance)
    monkeypatch.setattr(conflicts, "peer_rows", peer_rows)
    input = MemorySetInput(
        org="o",
        namespace="n",
        agent="-",
        memory_key="definition/arr",
        value="ARR",
        memory_type="definition",
    )
    w = set_commit._SetWrite(
        input=input, version=1, retyped_from="", existing=None, expires_at=None
    )
    policy = await set_commit._pg_peer_rows(w, SimpleNamespace(policy="auto_reinforce"))
    assert (policy, w.peer_rows, w.max_distance) == ("auto_reinforce", [], 0.0)


def test_a_bulk_definition_never_folds_into_another_row_of_its_bytes() -> None:
    from server.memory.app.bulk_rows import _bulk_skip_gate, _BulkWriteState

    def folded(memory_type: str) -> dict[str, str]:
        state = _BulkWriteState(existing_lookup_ok=True)
        entry = SimpleNamespace(memory_key="definition/arr", memory_type=memory_type)
        dupes = {"sha": ("arr_note", "mid-1")}  # the same bytes under another key
        _bulk_skip_gate(entry, "sha", state, dupes)
        return state.identity_duplicates

    assert folded("definition") == {}
    assert folded("semantic") == {"definition/arr": "arr_note"}


async def test_a_postgres_definition_verdict_captures_only_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_admission, write
    from server.memory.store import pg_memory

    async def capture(org: str, namespace: str) -> dict[tuple[str, str], int]:
        return {("o", "n"): 7}

    monkeypatch.setattr(pg_admission, "capture", capture)
    policy = {scope: 3 for scope in pg_memory.policy_scopes("o")}

    def w(memory_type: str) -> Any:
        return SimpleNamespace(
            input=SimpleNamespace(org="o", namespace="n", memory_type=memory_type), policy=policy
        )

    # A commit to the namespace cannot move a definition's verdict, so none is watched.
    assert await write._verdict_generations(w("definition")) == policy
    assert await write._verdict_generations(w("semantic")) == {("o", "n"): 7, **policy}


async def test_a_bulk_definition_has_no_reach_for_the_gap_check() -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    state = _BulkWriteState(existing_lookup_ok=True)
    state.floors = {"semantic": (0.8, 0.2)}
    row = {"org": "o", "namespace": "n", "embedding": [0.6, 0.8]}
    state.embedding_rows = [
        {**row, "memory_type": "definition", "memory_key": "definition/arr"},
        {**row, "memory_type": "semantic", "memory_key": "arr_note"},
    ]
    assert [key for _, key, _, _ in await bulk_set._reach_of(state)] == ["arr_note"]


async def test_a_postgres_batch_of_definitions_waits_for_no_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set, pg_admission
    from server.memory.app.bulk_rows import _BulkWriteState

    caught_up: list[str] = []

    async def catch_up(org: str, namespace: str, generations: Any) -> Any:
        caught_up.append(namespace)
        return generations

    async def verdicts(*a: Any, **kw: Any) -> dict[int, Any]:
        return {}

    monkeypatch.setattr(bulk_set.pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_admission, "catch_up", catch_up)
    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", verdicts)
    monkeypatch.setattr(bulk_set, "_apply_verdicts", lambda *a: None)

    async def run(memory_type: str) -> None:
        state = _BulkWriteState(existing_lookup_ok=True)
        state.memory_rows = [{"memory_type": memory_type, "memory_key": "k", "memory_id": "m"}]
        state.pending = [(0, "text", "excerpt", "key text")]
        clock = SimpleNamespace(mark=lambda _stage: None)
        await bulk_set._bulk_verdicts(SimpleNamespace(org="o", namespace="n"), state, [], [], clock)

    await run("definition")
    assert caught_up == []
    await run("semantic")
    assert caught_up == ["n"]


def test_consolidation_leaves_a_member_rewritten_as_a_definition() -> None:
    from server.memory.app.consolidate import _held_since
    from server.memory.store.rows import MemoryRow

    member = MemoryRow(
        memory_id="m1",
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s1",
        memory_key="arr_note",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        content_sha256="sha",
        pinned=False,
        created_at=datetime(2026, 1, 1),
        expires_at=None,
    )
    assert not _held_since(member, "sha")
    # A type-only rewrite keeps the hash; the merge still leaves the row.
    assert _held_since(member.model_copy(update={"memory_type": "definition"}), "sha")
