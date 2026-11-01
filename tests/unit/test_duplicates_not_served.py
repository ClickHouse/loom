"""Duplicates are not served: the search predicate, the
truth-read pool trim with canonical substitution, memory.list's skip,
memory.get's `canonical_key`, and memory.dedup_collapse's mirror repair.
Payloads stay byte-identical when nothing was hidden."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import MemoryGetInput, MemoryGetOutput, MemoryListInput
from server.memory.api.models_graph import MemoryDedupCollapseInput
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app import served_duplicates as sd
from server.memory.domain.search_types import RowKey
from server.memory.domain.temporal_sql import served_status_sql, status_predicate
from server.memory.store.dedup import CanonicalRow, DuplicatePair, DuplicateStatus

# ---------------------------------------------------------------- predicate


def test_status_predicate_hides_duplicates_by_default_and_restores_them_on_request() -> None:
    assert status_predicate(alias="e") == "e.status IN ('active', 'conflicted')"
    assert (
        status_predicate(alias="e", include_duplicates=True)
        == "e.status IN ('active', 'duplicate', 'conflicted')"
    )
    # knowledge_time replay keeps its superseded clause around the served set.
    kt = status_predicate("knowledge_time", alias="e", replaying=True)
    assert kt.startswith("(e.status IN ('active', 'conflicted') OR (e.status = 'superseded'")
    assert served_status_sql() == "status IN ('active', 'conflicted')"
    assert served_status_sql(alias="e", include_duplicates=True) == (
        "e.status IN ('active', 'duplicate', 'conflicted')"
    )


def test_build_where_carries_the_flag_from_the_search_query() -> None:
    from server.memory.domain.search_sql import _build_where
    from server.memory.domain.search_types import SearchQuery

    def q(**kw: Any) -> SearchQuery:
        return SearchQuery(
            org="o",
            namespace="n",
            scope="private",
            agent="a",
            embedding_model="m",
            query_vec=[0.1],
            top_k=5,
            **kw,
        )

    where, _ = _build_where(q())
    assert "e.status IN ('active', 'conflicted')" in where
    where, _ = _build_where(q(include_duplicates=True))
    assert "e.status IN ('active', 'duplicate', 'conflicted')" in where


def test_hide_duplicates_follows_the_call() -> None:
    assert sd.hide_duplicates(SimpleNamespace(include_duplicates=False)) is True
    assert sd.hide_duplicates(SimpleNamespace()) is True
    assert sd.hide_duplicates(SimpleNamespace(include_duplicates=True)) is False


# ---------------------------------------------------------------- search pool


def _hit(
    key: str,
    *,
    rank: float,
    source: str = "fact",
    ns: str = "",
    tags: Any = None,
    session: str = "s1",
    who: tuple[str, str, str] | None = ("a", "-", "s1"),  # a fact leg names its row
) -> Any:
    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"about {key}",
        distance=1 - rank,
        similarity=rank,
        rank_score=rank,
        source=source,
        status="active",
        namespace=ns,
        tags=tags,
        session_id=session,
    )
    hit._row_identity = who
    return hit


def _ctx(hits: list[Any], **input_kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"org": "o", "namespace": "ns", "agent": "a", "query": "q", "top_k": 3}
    base.update(input_kw)
    return SimpleNamespace(input=MemorySearchInput(**base), all_hits=list(hits), actual_work={})


@pytest.fixture
def truth(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake `loom.memory` truth: status per key and the canonical rows."""
    from server.memory import store as memory_repo

    env = SimpleNamespace(status={}, canon={}, status_calls=0, canon_calls=0)

    async def status_by_rows(
        org: str, rows: list[tuple[str, ...]], *, scope: Any = None, ch: Any = None
    ) -> list[DuplicateStatus]:
        env.status_calls += 1
        # as loom.memory: every stored row has a session, so a key without one matches none
        return [
            _status(row, **env.status[row.memory_key])
            for row in rows
            if row.memory_key in env.status and row.session_id
        ]

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        env.canon_calls += 1
        return [
            CanonicalRow.from_row(x)
            for x in [
                dict(env.canon[i], namespace=nss[0], canonical_id=i) for i in ids if i in env.canon
            ]
        ]

    async def status_by_writer_keys(
        org: str, keys: list[tuple[str, ...]], *, scope: Any = None, ch: Any = None
    ) -> list[DuplicateStatus]:
        env.status_calls += 1
        return [
            _status((ns, agent, user_id, "", k), **env.status[k])
            for ns, agent, user_id, k in keys
            if k in env.status
        ]

    monkeypatch.setattr(memory_repo, "status_by_rows", status_by_rows)
    monkeypatch.setattr(memory_repo, "status_by_writer_keys", status_by_writer_keys)
    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    return env


def _status(row: tuple[str, ...], **fields: Any) -> DuplicateStatus:
    names = ("namespace", "agent", "user_id", "session_id", "memory_key")
    return DuplicateStatus.from_row({**dict(zip(names, row, strict=True)), **fields})


def _dup(cid: str) -> dict[str, Any]:
    return {"row_status": "duplicate", "canonical_id": cid}


_ACTIVE = {"row_status": "active", "canonical_id": ""}


def _canon(key: str, value: Any = "the canonical text") -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": "semantic",
        "agent": "a",
        "value": json.dumps(value),
        "tags": ["kind:lesson"],
        "version": 7,
        "session_id": "s1",
        "user_id": "-",
        "source_class": "asserted",
        "created_at": "2026-09-01 00:00:00",
        "valid_at": "1970-01-01 00:00:00",
    }


@pytest.mark.asyncio
async def test_duplicate_is_replaced_by_its_canonical_in_its_slot(truth: SimpleNamespace) -> None:
    truth.status = {
        "dup_a": _dup("c-1"),
        "b": _ACTIVE,
    }
    truth.canon = {"c-1": _canon("canon_a")}
    ctx = _ctx(
        [_hit("dup_a", rank=0.9), _hit("b", rank=0.8), _hit("chunk:x", rank=0.7, source="chunk")]
    )
    await sd.hide_pool_duplicates(ctx)
    keys = [h.memory_key for h in ctx.all_hits]
    assert keys == ["canon_a", "b", "chunk:x"]
    seated = ctx.all_hits[0]
    assert (
        seated.resolved_from == "dup_a" and seated.rank_score == 0.9 and seated.status == "active"
    )
    # the excerpt is built the way the canonical's own embedding row was: value first, key suffix
    assert seated.content_excerpt == '"the canonical text" (#canon_a)' and seated.version == 7
    assert seated.valid_at == "" and seated.tags is None  # tags stay off the wire when not asked
    assert ctx.actual_work == {"duplicates_hidden": 1, "duplicates_resolved": 1}
    assert truth.status_calls == 1 and truth.canon_calls == 1


@pytest.mark.asyncio
async def test_duplicate_whose_canonical_is_already_in_the_pool_is_dropped(
    truth: SimpleNamespace,
) -> None:
    truth.status = {
        "dup_a": _dup("c-1"),
        "canon_a": _ACTIVE,
    }
    truth.canon = {"c-1": _canon("canon_a")}
    ctx = _ctx(
        [
            _hit("canon_a", rank=0.95, who=("a", "-", "s1")),
            _hit("dup_a", rank=0.9),
            _hit("z", rank=0.5),
        ]
    )
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["canon_a", "z"]
    assert ctx.all_hits[0].resolved_from is None
    assert ctx.actual_work == {"duplicates_hidden": 1, "duplicates_resolved": 0}


@pytest.mark.asyncio
async def test_two_duplicates_of_one_canonical_seat_it_once(truth: SimpleNamespace) -> None:
    truth.status = {"dup_a": _dup("c-1"), "dup_b": _dup("c-1")}
    truth.canon = {"c-1": _canon("canon_a")}
    ctx = _ctx([_hit("dup_a", rank=0.9), _hit("dup_b", rank=0.8)])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["canon_a"]
    assert ctx.all_hits[0].resolved_from == "dup_a"


@pytest.mark.asyncio
async def test_duplicate_with_no_live_canonical_is_hidden_without_substitute(
    truth: SimpleNamespace,
) -> None:
    truth.status = {"dup_a": _dup("gone")}
    ctx = _ctx([_hit("dup_a", rank=0.9), _hit("b", rank=0.8)])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["b"]
    assert ctx.actual_work == {"duplicates_hidden": 1, "duplicates_resolved": 0}


@pytest.mark.asyncio
async def test_a_hit_naming_only_its_writer_takes_its_writers_key_status(
    truth: SimpleNamespace,
) -> None:
    # a folded set chunk knows its writer but not its row: the writer's key status decides it
    truth.status = {"dup_a": _dup("c-1"), "b": _ACTIVE}
    named = _hit("b", rank=0.8, session="s1", who=("a", "u", "s1"))
    chunk = _hit("dup_a", rank=0.9, session="", who=None)
    chunk._row_writer = ("a", "-")
    ctx = _ctx([chunk, named])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["b"]


@pytest.mark.asyncio
async def test_a_canonical_a_folded_chunk_of_its_writer_stands_for_is_not_seated_twice(
    truth: SimpleNamespace,
) -> None:
    truth.status = {"dup_a": _dup("c-1"), "canon_a": _ACTIVE}
    truth.canon = {"c-1": _canon("canon_a")}  # written by ("a", "-")
    chunk = _hit("canon_a", rank=0.5, session="", who=None)
    chunk._row_writer = ("a", "-")
    ctx = _ctx([_hit("dup_a", rank=0.9), chunk])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["canon_a"]
    assert ctx.all_hits[0] is chunk


@pytest.mark.asyncio
async def test_an_active_row_is_served(truth: SimpleNamespace) -> None:
    truth.status = {"k": _ACTIVE}
    ctx = _ctx([_hit("k", rank=0.9)])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["k"] and ctx.actual_work == {}
    assert truth.canon_calls == 0


@pytest.mark.asyncio
async def test_include_duplicates_and_as_of_and_failures(
    truth: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth.status = {"dup_a": _dup("c-1")}
    truth.canon = {"c-1": _canon("canon_a")}
    # include_duplicates: the pool is untouched and the truth is never read.
    ctx = _ctx([_hit("dup_a", rank=0.9)], include_duplicates=True)
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["dup_a"] and truth.status_calls == 0
    # as_of: hidden, not substituted (the canonical's window was not checked at the anchor).
    ctx = _ctx(
        [_hit("dup_a", rank=0.9), _hit("b", rank=0.5)], as_of=datetime(2026, 9, 1, tzinfo=UTC)
    )
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["b"] and truth.canon_calls == 0
    # a failed truth read leaves the pool as ranked.
    from server.memory import store as memory_repo

    async def boom(*a: Any, **kw: Any) -> list[dict]:
        raise RuntimeError("ch down")

    monkeypatch.setattr(memory_repo, "status_by_rows", boom)
    ctx = _ctx([_hit("dup_a", rank=0.9)])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["dup_a"] and ctx.actual_work == {}


@pytest.mark.asyncio
async def test_cross_namespace_pool_keys_the_truth_per_namespace(
    truth: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory import store as memory_repo

    async def status_by_rows(
        org: str, rows: list[tuple[str, ...]], *, scope: Any = None, ch: Any = None
    ) -> list[DuplicateStatus]:
        assert sorted({row[0] for row in rows}) == ["ns", "other"]
        return [_status(row, **_dup("c-9")) for row in rows if row[0] == "other"]

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        assert nss == ["other"] and ids == ["c-9"]
        return [
            CanonicalRow.from_row(x)
            for x in [dict(_canon("canon_k"), namespace="other", canonical_id="c-9")]
        ]

    monkeypatch.setattr(memory_repo, "status_by_rows", status_by_rows)
    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    ctx = _ctx([_hit("k", rank=0.9, ns="other"), _hit("k", rank=0.8, ns="ns")])
    await sd.hide_pool_duplicates(ctx)
    assert [(h.namespace, h.memory_key) for h in ctx.all_hits] == [
        ("other", "canon_k"),
        ("ns", "k"),
    ]


def test_resolved_from_is_absent_from_the_payload_unless_seated() -> None:
    plain = _hit("a", rank=0.5).model_dump()
    assert "resolved_from" not in plain
    seated = _hit("a", rank=0.5).model_copy(update={"resolved_from": "d"}).model_dump()
    assert seated["resolved_from"] == "d"
    assert MemorySearchInput(org="o", namespace="n", query="q").include_duplicates is False


# ---------------------------------------------------------------- memory.list


@pytest.mark.asyncio
async def test_list_skips_duplicates_by_default_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import query as q

    seen: dict[str, Any] = {}

    async def list_by_namespace(identity: Any, **kw: Any) -> list[Any]:
        seen["list"] = kw
        return []

    async def count_by_namespace(identity: Any, **kw: Any) -> int:
        seen.setdefault("count", []).append(kw)
        return 0

    monkeypatch.setattr(memory_repo, "list_by_namespace", list_by_namespace)
    monkeypatch.setattr(memory_repo, "count_by_namespace", count_by_namespace)

    async def namespace_version(org: str, ns: str, **kw: Any) -> int:  # The watermark read
        return 0

    monkeypatch.setattr(memory_repo, "namespace_version", namespace_version)
    monkeypatch.setattr(q, "_record_read_exposure", lambda *a, **k: None)
    await q.handle_memory_list(MemoryListInput(org="o", namespace="n", agent="a"))
    assert seen["list"]["include_duplicates"] is False
    assert all(c["include_duplicates"] is False for c in seen["count"])
    await q.handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", include_duplicates=True)
    )
    assert seen["list"]["include_duplicates"] is True

    # score path: the predicate is in the statement itself.
    sqls: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            sqls.append(sql)
            return []

        async def query_one(self, sql: str, params: Any = None) -> dict:
            sqls.append(sql)
            return {"n": 0}

    monkeypatch.setattr(q, "get_client", lambda: _CH())
    await q.handle_memory_list(MemoryListInput(org="o", namespace="n", agent="a", sort_by="score"))
    assert all("status != 'duplicate'" in s for s in sqls)
    sqls.clear()
    await q.handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", sort_by="score", include_duplicates=True)
    )
    assert not any("status != 'duplicate'" in s for s in sqls)


@pytest.mark.asyncio
async def test_repo_list_and_count_carry_the_predicate_only_when_asked() -> None:
    from server.memory import store as memory_repo

    sqls: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            sqls.append(sql)
            return []

        async def query_one(self, sql: str, params: Any = None) -> dict:
            sqls.append(sql)
            return {"n": 0}

    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    await memory_repo.list_by_namespace(ident, ch=_CH(), scope=WHOLE_NAMESPACE)
    await memory_repo.count_by_namespace(ident, ch=_CH(), scope=WHOLE_NAMESPACE)
    assert not any("duplicate" in s for s in sqls), "other callers' statements are unchanged"
    sqls.clear()
    await memory_repo.list_by_namespace(
        ident, ch=_CH(), include_duplicates=False, scope=WHOLE_NAMESPACE
    )
    await memory_repo.count_by_namespace(
        ident, ch=_CH(), include_duplicates=False, scope=WHOLE_NAMESPACE
    )
    assert all("status != 'duplicate'" in s for s in sqls)


# ---------------------------------------------------------------- memory.get / bulk_get


def _row(status: str = "active", duplicate_of: str = "") -> SimpleNamespace:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return SimpleNamespace(
        memory_id="11111111-1111-1111-1111-111111111111",
        memory_key="k",
        value={"t": 1},
        memory_type="semantic",
        confidence=1.0,
        version=3,
        created_at=now,
        updated_at=now,
        expires_at=None,
        on_expire="",
        expire_reason="",
        valid_at=None,
        valid_to=None,
        temporal_anchor=None,
        temporal_confidence=0.0,
        pinned=False,
        actor="",
        source_class="",
        sensitivity="",
        recall_count=0,
        last_recalled_at=None,
        evidence_count=1,
        agent="a",
        user_id="-",
        session_id="-",
        reinforcement_count=1,
        last_reinforced_at=None,
        tags=[],
        entities=[],
        status=status,
        duplicate_of=duplicate_of,
        source_trace="",
        run_id="-",
        verified_by="",
        verified_at=None,
        verified_sha256="",
        grounding="",
        evidence="",
        char_limit=None,
        mentioned_at=None,
    )


@pytest.fixture
def get_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from server.memory import store as memory_repo

    e = SimpleNamespace(row=_row(), canon={}, canon_calls=0)

    async def get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return e.row

    async def get_many_by_keys(identity: Any, keys: list[str], **kw: Any) -> list[Any]:
        return [e.row]

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        e.canon_calls += 1
        return [
            CanonicalRow.from_row(x)
            for x in [
                {"namespace": nss[0], "canonical_id": i, "memory_key": e.canon[i]}
                for i in ids
                if i in e.canon
            ]
        ]

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(memory_repo, "get_many_by_keys", get_many_by_keys)
    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    monkeypatch.setattr("server.memory.app.query._record_read_exposure", lambda *a, **k: None)
    return e


@pytest.mark.asyncio
async def test_get_names_the_canonical_of_a_duplicate_and_nothing_else(
    get_env: SimpleNamespace,
) -> None:
    from server.memory.api.models_bulk import MemoryBulkGetInput
    from server.memory.app import handlers as mem
    from server.memory.app.query import handle_memory_bulk_get

    inp = MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k")
    out = await mem.handle_memory_get(inp)
    assert out.status == "active" and "canonical_key" not in out.model_dump()
    assert get_env.canon_calls == 0

    get_env.row = _row("duplicate", "c-1")
    get_env.canon = {"c-1": "canon_k"}
    out = await mem.handle_memory_get(inp)
    assert out.model_dump()["canonical_key"] == "canon_k" and out.value == {"t": 1}
    bulk = await handle_memory_bulk_get(
        MemoryBulkGetInput(org="o", namespace="n", agent="a", memory_keys=["k"])
    )
    assert bulk.results["k"].canonical_key == "canon_k"

    get_env.canon = {}  # canonical gone: the field says so with ""
    out = await mem.handle_memory_get(inp)
    assert out.model_dump()["canonical_key"] == ""


def test_get_output_payload_is_unchanged_when_the_key_is_unset() -> None:
    assert "canonical_key" not in MemoryGetOutput(found=True).model_dump()
    assert "canonical_key" not in json.loads(MemoryGetOutput(found=False).model_dump_json())


# ---------------------------------------------------------------- memory.dedup_collapse


@pytest.fixture
def collapse_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from server.memory import store as memory_repo

    e = SimpleNamespace(pairs=[], canon={}, mirror=[], restamped=[], prefix=None)

    async def find_duplicate_pairs(
        org: str, ns: str, *, key_prefix: str = "", limit: int = 500, ch: Any = None
    ) -> list[DuplicatePair]:
        e.prefix = key_prefix
        return [
            DuplicatePair.from_row(x)
            for x in [p for p in e.pairs if p["memory_key"].startswith(key_prefix)][:limit]
        ]

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        return [
            CanonicalRow.from_row(x)
            for x in [
                {"namespace": ns, "canonical_id": i, "memory_key": e.canon[i]}
                for ns in nss
                for i in ids
                if i in e.canon
            ]
        ]

    async def embedding_mirror_for_keys(
        org: str, ns: str, keys: list[str], *, ch: Any = None
    ) -> list[dict]:
        return [r for r in e.mirror if r["memory_key"] in keys]

    async def restamp_embedding_rows(
        rows: list[dict], *, status: str, version: int, ch: Any = None
    ) -> int:
        e.restamped.extend((r["memory_key"], status) for r in rows)
        return len(rows)

    monkeypatch.setattr(memory_repo, "find_duplicate_pairs", find_duplicate_pairs)
    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    monkeypatch.setattr(memory_repo, "embedding_mirror_for_keys", embedding_mirror_for_keys)
    monkeypatch.setattr(memory_repo, "restamp_embedding_rows", restamp_embedding_rows)
    return e


def _pair(key: str, cid: str, session: str = "-") -> dict[str, Any]:
    return {
        "memory_id": f"id-{key}",
        "memory_key": key,
        "agent": "a",
        "user_id": "-",
        "session_id": session,
        "duplicate_of": cid,
    }


def _mirror(key: str, status: str, session: str = "-") -> dict[str, Any]:
    return {
        "memory_key": key,
        "agent": "a",
        "user_id": "-",
        "session_id": session,
        "status": status,
    }


@pytest.mark.asyncio
async def test_collapse_counts_pairs_orphans_and_stale_mirror_rows_and_writes_only_when_asked(
    collapse_env: SimpleNamespace,
) -> None:
    from server.memory.app.dedup import handle_memory_dedup_collapse

    collapse_env.pairs = [
        _pair("lesson_a", "c-1"),
        _pair("lesson_b", "c-2"),
        _pair("finding_c", "gone"),
    ]
    collapse_env.canon = {"c-1": "lesson_x", "c-2": "lesson_y"}
    collapse_env.mirror = [
        _mirror("lesson_a", "active"),  # stale: the truth says duplicate
        _mirror("lesson_b", "duplicate"),  # already right
        _mirror(
            "lesson_b", "active", session="s2"
        ),  # another identity's row under the key: not ours
    ]
    out = await handle_memory_dedup_collapse(MemoryDedupCollapseInput(org="o", namespace="n"))
    assert (out.pairs, out.canonical_live, out.orphans, out.mirror_stale, out.restamped) == (
        3,
        2,
        1,
        1,
        0,
    )
    assert out.dry_run is True and collapse_env.restamped == []
    assert [(p.duplicate_key, p.canonical_key) for p in out.sample] == [
        ("lesson_a", "lesson_x"),
        ("lesson_b", "lesson_y"),
    ]

    out = await handle_memory_dedup_collapse(
        MemoryDedupCollapseInput(org="o", namespace="n", dry_run=False)
    )
    assert out.restamped == 1 and collapse_env.restamped == [("lesson_a", "duplicate")]

    out = await handle_memory_dedup_collapse(
        MemoryDedupCollapseInput(org="o", namespace="n", prefix="finding_")
    )
    assert collapse_env.prefix == "finding_" and (out.pairs, out.orphans) == (1, 1)


def test_collapse_is_a_writer_op_hidden_from_mcp() -> None:
    from server.memory.api.ops_graph import memory_dedup_collapse
    from server.operations.base import Role
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    assert memory_dedup_collapse.required_role is Role.WRITER
    assert memory_dedup_collapse.audit is True
    assert "memory.dedup_collapse" in _MCP_HIDDEN_OPS


@pytest.mark.asyncio
async def test_a_canonical_outside_the_read_scope_is_never_seated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U's duplicate (its embedding still `active`) pointing at V's canonical:
    a search pinned to U drops the duplicate and never shows V's key or text."""
    from server.identity.domain.read_scope import ReadFilters, row_in_read_scope
    from server.memory import store as memory_repo

    only_u = ReadFilters(scope="own_user", user_id="u", session_id=None)
    seen: list[Any] = []

    async def status_by_rows(
        org: str, rows: list[tuple[str, ...]], *, scope: Any = None, ch: Any = None
    ) -> list[DuplicateStatus]:
        seen.append(scope)
        return [_status(row, **_dup("c-v")) for row in rows if row[4] == "k"]

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        seen.append(scope)
        row = dict(_canon("v_key", "v private text"), namespace=nss[0], canonical_id="c-v")
        row["user_id"] = "v"
        if scope is not None and not row_in_read_scope(scope, row["user_id"], row["session_id"]):
            return []
        return [CanonicalRow.from_row(row)]

    monkeypatch.setattr(memory_repo, "status_by_rows", status_by_rows)
    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    monkeypatch.setattr(sd, "request_scope", lambda: only_u)
    ctx = _ctx([_hit("k", rank=0.9), _hit("b", rank=0.5)])
    await sd.hide_pool_duplicates(ctx)
    assert seen == [only_u, only_u]
    assert [h.memory_key for h in ctx.all_hits] == ["b"]
    dumped = json.dumps([h.model_dump() for h in ctx.all_hits], default=str)
    assert "v_key" not in dumped and "v private text" not in dumped


@pytest.mark.asyncio
async def test_repo_duplicate_reads_carry_the_read_scope() -> None:
    from server.identity.domain.read_scope import ReadFilters
    from server.memory import store as memory_repo

    sqls: list[tuple[str, dict]] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            sqls.append((sql, params or {}))
            return []

    one_session = ReadFilters(scope="own_session", user_id="u", session_id="s")
    cid = "11111111-1111-1111-1111-111111111111"
    await memory_repo.status_by_rows(
        "o", [RowKey("n", "a", "u", "s", "k")], scope=one_session, ch=_CH()
    )
    await memory_repo.canonical_rows_by_ids("o", ["n"], [cid], scope=one_session, ch=_CH())
    await memory_repo.status_by_writer_keys(
        "o", [("n", "a", "u", "k")], scope=one_session, ch=_CH()
    )
    for sql, params in sqls:
        assert "user_id = {scope_uid:String}" in sql and "session_id = {scope_sid:String}" in sql
        assert params["scope_uid"] == "u" and params["scope_sid"] == "s"


# ---------------------------------------------------------------- repo statements


@pytest.mark.asyncio
async def test_repo_readers_never_alias_an_aggregate_to_its_source_column() -> None:
    from server.memory import store as memory_repo

    sqls: list[tuple[str, dict]] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            sqls.append((sql, params or {}))
            return []

    ch = _CH()
    whole = WHOLE_NAMESPACE
    rows = [RowKey("n", "a", "-", "-", k) for k in ("k2", "k1", "k1")]
    await memory_repo.status_by_rows(
        "o", [*rows, RowKey("n", "a", "-", "-", "")], scope=whole, ch=ch
    )
    await memory_repo.canonical_rows_by_ids(
        "o", ["n"], ["not-a-uuid", "11111111-1111-1111-1111-111111111111"], scope=whole, ch=ch
    )
    assert await memory_repo.canonical_rows_by_ids("o", ["n"], ["junk"], scope=whole, ch=ch) == []
    await memory_repo.find_duplicate_pairs("o", "n", key_prefix="lesson_", ch=ch)
    await memory_repo.embedding_mirror_for_keys("o", "n", ["k"], ch=ch)
    assert len(sqls) == 4
    assert sqls[0][1]["rows"] == sorted(set(rows)) and "AS canonical_id" in sqls[0][0]
    assert (
        sqls[1][1]["ids"] == ["11111111-1111-1111-1111-111111111111"]
        and "status = 'active'" in sqls[1][0]
    )
    assert "startsWith(memory_key, {prefix:String})" in sqls[2][0]
    for sql, _ in sqls:
        assert "AS status" not in sql and "AS version" not in sql
    # the two search-path reads never pay the read-your-writes Keeper round-trip
    assert all("select_sequential_consistency = 0" in sqls[i][0] for i in (0, 1))
    assert await memory_repo.restamp_embedding_rows([], status="duplicate", version=1, ch=ch) == 0


@pytest.mark.parametrize(
    "dup_row, live_row",
    [
        (("a", "u", "s1"), ("a", "u", "s2")),  # two sessions of one user
        (("a", "u1", "-"), ("a", "u2", "-")),  # two users, no session
    ],
)
@pytest.mark.asyncio
async def test_a_duplicate_is_hidden_though_another_writer_holds_its_key_active(
    truth: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    dup_row: tuple[str, str, str],
    live_row: tuple[str, str, str],
) -> None:
    from server.memory import store as memory_repo

    async def status_by_rows(
        org: str, rows: list[tuple[str, ...]], *, scope: Any = None, ch: Any = None
    ) -> list[DuplicateStatus]:
        return [
            _status(("ns", *dup_row, "k"), **_dup("c-1")),
            _status(("ns", *live_row, "k"), **_ACTIVE),
        ]

    monkeypatch.setattr(memory_repo, "status_by_rows", status_by_rows)
    truth.canon = {"c-1": _canon("canon_a")}
    ctx = _ctx([_hit("k", rank=0.9, who=dup_row), _hit("k", rank=0.8, who=live_row)])
    await sd.hide_pool_duplicates(ctx)
    assert [h.memory_key for h in ctx.all_hits] == ["canon_a", "k"]
    assert ctx.all_hits[1]._row_identity == live_row
