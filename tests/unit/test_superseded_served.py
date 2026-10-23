"""A superseded row is served only when asked, and names its successor: the
status predicate under both widening flags, the per-call / deployment
switch, the pool annotation, the ask label and note, the superseded-row
drop under the flag, memory.list's predicate and entry
status, and the wire shape when nothing was asked."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_crud import MemoryListInput
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app import served_superseded as ss
from server.memory.app.ask import prompts, rendering, supersession
from server.memory.domain.temporal_sql import served_status_sql, served_statuses, status_predicate
from server.memory.store.dedup import SupersededSuccessor

# ---------------------------------------------------------------- predicate


def test_served_statuses_widen_by_flag() -> None:
    assert served_statuses() == ("active", "conflicted")
    assert served_statuses(include_duplicates=True) == ("active", "duplicate", "conflicted")
    assert served_statuses(include_superseded=True) == ("active", "conflicted", "superseded")
    assert served_statuses(include_duplicates=True, include_superseded=True) == (
        "active",
        "duplicate",
        "conflicted",
        "superseded",
    )


def test_status_predicate_and_served_sql_carry_the_flag() -> None:
    assert served_status_sql(alias="e") == "e.status IN ('active', 'conflicted')"
    assert served_status_sql(alias="e", include_superseded=True) == (
        "e.status IN ('active', 'conflicted', 'superseded')"
    )
    assert status_predicate(alias="e", include_superseded=True) == (
        "e.status IN ('active', 'conflicted', 'superseded')"
    )
    # knowledge_time replay: the anchor clause is redundant once every
    # superseded row is admitted, and the widened base stands alone.
    kt_off = status_predicate("knowledge_time", alias="e", replaying=True)
    assert kt_off.startswith("(e.status IN ('active', 'conflicted') OR (e.status = 'superseded'")
    kt_on = status_predicate("knowledge_time", alias="e", replaying=True, include_superseded=True)
    assert kt_on == "e.status IN ('active', 'conflicted', 'superseded')"


def test_search_query_threads_the_flag_into_the_leg_where() -> None:
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
    where, _ = _build_where(q(include_superseded=True))
    assert "e.status IN ('active', 'conflicted', 'superseded')" in where


# ---------------------------------------------------------------- the switch


def test_hide_superseded_follows_the_call() -> None:
    assert ss.hide_superseded(SimpleNamespace(include_superseded=False)) is True
    assert ss.hide_superseded(SimpleNamespace()) is True
    assert ss.hide_superseded(SimpleNamespace(include_superseded=True)) is False


def test_served_flags_names_both_widenings() -> None:
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    assert ss.served_flags(inp) == {
        "include_duplicates": False,
        "include_superseded": False,
        "include_proposed": False,
    }
    inp = MemorySearchInput(
        org="o",
        namespace="n",
        agent="a",
        query="q",
        include_superseded=True,
        include_duplicates=True,
    )
    assert ss.served_flags(inp) == {
        "include_duplicates": True,
        "include_superseded": True,
        "include_proposed": False,
    }
    # Proposals widen only on the call's own flag.
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q", include_proposed=True)
    assert ss.served_flags(inp)["include_proposed"] is True


# ---------------------------------------------------------------- the pool


def _hit(key: str, *, rank: float, status: str = "active", source: str = "fact") -> Any:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"about {key}",
        distance=1 - rank,
        similarity=rank,
        rank_score=rank,
        source=source,
        status=status,
    )


def _ctx(hits: list[Any], **input_kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"org": "o", "namespace": "ns", "agent": "a", "query": "q", "top_k": 3}
    base.update(input_kw)
    return SimpleNamespace(input=MemorySearchInput(**base), all_hits=list(hits), actual_work={})


@pytest.fixture
def successors(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from server.memory import store as memory_repo

    env = SimpleNamespace(rows=[], calls=0, fail=False)

    async def superseded_successors(
        org: str, nss: list[str], keys: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[SupersededSuccessor]:
        env.calls += 1
        env.last = (org, nss, keys)
        env.scope = scope
        if env.fail:
            raise RuntimeError("boom")
        return [SupersededSuccessor.from_row(x) for x in list(env.rows)]

    monkeypatch.setattr(memory_repo, "superseded_successors", superseded_successors)
    return env


@pytest.mark.asyncio
async def test_annotation_names_the_successor_and_counts_only_when_asked(
    successors: SimpleNamespace,
) -> None:
    successors.rows = [
        {
            "namespace": "ns",
            "memory_key": "deploy_window",
            "superseded_by": "id-9",
            "successor_key": "deploy_window",
            "superseded_at": "2026-09-10 12:00:00",
        },
        {
            "namespace": "ns",
            "memory_key": "db_engine",
            "superseded_by": "id-7",
            "successor_key": "",
            "superseded_at": "2026-09-01 08:00:00",
        },
    ]
    ctx = _ctx(
        [
            _hit("deploy_window", rank=0.9, status="superseded"),
            _hit("db_engine", rank=0.8, status="superseded"),
            _hit("owner", rank=0.7),
            _hit("chunk:x", rank=0.6, status="superseded", source="chunk"),
        ],
        include_superseded=True,
    )
    await ss.annotate_pool_superseded(ctx)
    assert successors.calls == 1 and successors.last == (
        "o",
        ["ns"],
        ["deploy_window", "db_engine"],
    )
    a, b, c, d = ctx.all_hits
    assert (a.superseded_by, a.superseded_at) == ("deploy_window", "2026-09-10 12:00:00")
    assert (b.superseded_by, b.superseded_at) == (
        "id-7",
        "2026-09-01 08:00:00",
    )  # successor gone: the id
    assert c.superseded_by is None and d.superseded_by is None
    assert ctx.actual_work == {"superseded_served": 2}


@pytest.mark.asyncio
async def test_each_writers_superseded_row_names_its_own_successor(
    successors: SimpleNamespace,
) -> None:
    def row(user_id: str, successor: str) -> dict[str, str]:
        return {
            "namespace": "ns",
            "agent": "a",
            "user_id": user_id,
            "session_id": "-",
            "memory_key": "k",
            "superseded_by": successor,
            "successor_key": successor,
            "superseded_at": "2026-09-10 12:00:00",
        }

    successors.rows = [row("u1", "k_u1"), row("u2", "k_u2")]
    hits = [_hit("k", rank=0.9, status="superseded"), _hit("k", rank=0.8, status="superseded")]
    hits[0]._row_identity, hits[1]._row_identity = ("a", "u2", "-"), ("a", "u1", "-")
    await ss.annotate_pool_superseded(_ctx(hits, include_superseded=True))
    assert [h.superseded_by for h in hits] == ["k_u2", "k_u1"]


@pytest.mark.asyncio
async def test_a_bounded_read_names_no_successor_outside_its_scope(
    successors: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The successor read gets the request scope, and a successor it did not
    resolve (another user's row) is not named by its raw id either."""
    from server.identity.domain.read_scope import ReadFilters

    only_u = ReadFilters(scope="own_user", user_id="u", session_id=None)
    monkeypatch.setattr(ss, "request_scope", lambda: only_u)
    successors.rows = [
        {
            "namespace": "ns",
            "memory_key": "db_engine",
            "superseded_by": "v-successor-id",
            "successor_key": "",
            "superseded_at": "2026-09-01 08:00:00",
        }
    ]
    ctx = _ctx([_hit("db_engine", rank=0.8, status="superseded")], include_superseded=True)
    await ss.annotate_pool_superseded(ctx)
    assert successors.scope is only_u
    assert ctx.all_hits[0].superseded_by == "" and ctx.all_hits[0].superseded_at


@pytest.mark.asyncio
async def test_successor_statement_scopes_both_reads() -> None:
    from server.identity.domain.read_scope import ReadFilters
    from server.memory import store as memory_repo

    sqls: list[tuple[str, dict]] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            sqls.append((sql, params or {}))
            return []

    one_session = ReadFilters(scope="own_session", user_id="u", session_id="s")
    await memory_repo.superseded_successors("o", ["n"], ["k"], scope=one_session, ch=_CH())
    [(sql, params)] = sqls
    # once in the superseded-row read, once in the successor join
    assert sql.count("user_id = {scope_uid:String}") == 2
    assert sql.count("session_id = {scope_sid:String}") == 2
    assert params["scope_uid"] == "u" and params["scope_sid"] == "s"


@pytest.mark.asyncio
async def test_annotation_is_a_no_op_without_the_flag_or_without_a_superseded_hit(
    successors: SimpleNamespace,
) -> None:
    ctx = _ctx([_hit("k", rank=0.9, status="superseded")])  # flag off: the leg never serves one
    await ss.annotate_pool_superseded(ctx)
    assert successors.calls == 0 and ctx.actual_work == {}
    ctx = _ctx([_hit("k", rank=0.9)], include_superseded=True)
    await ss.annotate_pool_superseded(ctx)
    assert successors.calls == 0 and ctx.actual_work == {}


@pytest.mark.asyncio
async def test_annotation_fails_open(successors: SimpleNamespace) -> None:
    successors.fail = True
    ctx = _ctx([_hit("k", rank=0.9, status="superseded")], include_superseded=True)
    await ss.annotate_pool_superseded(ctx)
    h = ctx.all_hits[0]
    assert h.status == "superseded" and h.superseded_by is None
    assert ctx.actual_work == {"superseded_served": 1}


def test_wire_payload_carries_the_successor_only_on_a_superseded_hit() -> None:
    plain = json.loads(_hit("k", rank=0.9).model_dump_json())
    assert "superseded_by" not in plain and "superseded_at" not in plain
    old = _hit("k", rank=0.9, status="superseded")
    old.superseded_by, old.superseded_at = "k2", "2026-09-10"
    data = json.loads(old.model_dump_json())
    assert data["superseded_by"] == "k2" and data["superseded_at"] == "2026-09-10"


# ---------------------------------------------------------------- the ask


def _row(key: str, status: str = "active", **kw: Any) -> SimpleNamespace:
    base = dict(
        memory_key=key,
        content_excerpt="the value",
        memory_type="semantic",
        source_class="asserted",
        valid_at="2026-01-02",
        created_at="",
        about_other=False,
        status=status,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_superseded_label_and_memory_line() -> None:
    assert ss.superseded_label(_row("k")) == ""
    assert ss.superseded_label(_row("k", "superseded")) == "[superseded]"
    old = _row("k", "superseded", superseded_at="2026-09-10 12:00:00", superseded_by="k_v2")
    assert ss.superseded_label(old) == "[superseded 2026-09-10; replaced by k_v2]"
    line = rendering._memory_line(old, nonce="abcd1234")
    assert "[superseded 2026-09-10; replaced by k_v2] <<memory k abcd1234>>\nthe value" in line
    cur = rendering._memory_line(_row("k"), nonce="abcd1234")
    assert "superseded" not in cur


def test_window_note_only_when_a_superseded_row_is_present() -> None:
    assert prompts._superseded_notes([_row("a"), _row("b")]) == []
    notes = prompts._superseded_notes([_row("a"), _row("b", "superseded")])
    assert notes == [prompts._SUPERSEDED_NOTE] and "NOT current" in notes[0]
    # the combined note list keeps its order: question, tier, superseded
    combined = prompts._window_notes("what is the deploy window?", [_row("b", "superseded")])
    assert combined[-1] == prompts._SUPERSEDED_NOTE


def test_drop_superseded_follows_the_call() -> None:
    assert supersession._drop_superseded(SimpleNamespace(include_superseded=False)) is True
    assert supersession._drop_superseded(SimpleNamespace(include_superseded=True)) is False


def test_ask_input_forwards_the_flag_to_the_retrieval_leg() -> None:
    from server.memory.api.models_ask import MemoryAskInput

    src = Path("server/memory/app/ask/handler.py").read_text()
    assert "include_superseded=input.include_superseded" in src
    assert MemoryAskInput(org="o", namespace="n", agent="a", query="q").include_superseded is False


# ---------------------------------------------------------------- memory.list


class _ListCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return list(self.rows)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((sql, dict(params or {})))
        return {"n": len(self.rows)}


def _score_row(key: str, status: str) -> dict[str, Any]:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    return {
        "memory_key": key,
        "memory_type": "semantic",
        "confidence": 1.0,
        "version": 1,
        "created_at": now,
        "expires_at": None,
        "valid_at": None,
        "pinned": False,
        "status": status,
        "evidence_count": 2,
        "recall_count": 4,
        "last_recalled_at": None,
        "tags": [],
        "entities": [],
        "source_trace": "",
        "verified_by": "",
        "verified_sha256": "",
        "content_sha256": "",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "value": json.dumps({"t": 1}),
        "value_truncated": 0,
    }


@pytest.mark.asyncio
async def test_list_score_path_hides_superseded_by_default_and_serves_with_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import query as mq

    ch = _ListCH([_score_row("k_old", "superseded"), _score_row("k", "active")])
    monkeypatch.setattr(mq, "get_client", lambda: ch)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a, **k: None)
    await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", sort_by="score")
    )
    sql, _ = ch.calls[0]
    assert "status != 'superseded'" in sql and "status != 'duplicate'" in sql
    ch.calls.clear()
    out = await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", sort_by="score", include_superseded=True)
    )
    sql, _ = ch.calls[0]
    assert "status != 'superseded'" not in sql and "status != 'duplicate'" in sql
    assert [e.status for e in out.entries] == ["superseded", "active"]


@pytest.mark.asyncio
async def test_list_recency_path_passes_the_flag_to_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import query as mq

    seen: dict[str, Any] = {}

    async def list_by_namespace(identity: Any, **kw: Any) -> list[Any]:
        seen["list"] = kw
        return []

    async def count_by_namespace(identity: Any, **kw: Any) -> int:
        seen.setdefault("counts", []).append(kw)
        return 0

    monkeypatch.setattr(memory_repo, "list_by_namespace", list_by_namespace)
    monkeypatch.setattr(memory_repo, "count_by_namespace", count_by_namespace)

    async def namespace_version(org: str, ns: str, **kw: Any) -> int:  # The watermark read
        return 0

    monkeypatch.setattr(memory_repo, "namespace_version", namespace_version)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a, **k: None)
    await mq._handle_memory_list(MemoryListInput(org="o", namespace="n", agent="a"))
    assert (
        seen["list"]["include_superseded"] is False and seen["list"]["include_duplicates"] is False
    )
    assert all(c["include_superseded"] is False for c in seen["counts"])
    await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", include_superseded=True)
    )
    assert seen["list"]["include_superseded"] is True


def test_repository_predicates_follow_the_flag() -> None:
    import inspect

    from server.memory import store as memory_repo

    for fn in (memory_repo.list_by_namespace, memory_repo.count_by_namespace):
        src = inspect.getsource(fn)
        assert "include_superseded: bool = True" in src
        assert "status != 'superseded'" in src
    assert "status = 'superseded'" in inspect.getsource(memory_repo.superseded_successors)
