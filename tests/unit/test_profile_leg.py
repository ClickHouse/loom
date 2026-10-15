"""Loom_search's profile leg finds a section by meaning, reads
only what it needs, and never scores a stale vector.

"what do I like to drink?" must find the profile line "likes tea" (a
substring-only leg cannot), without reading every column of every row of the
searched namespaces under FINAL on each search. Profile vectors must follow a
section's current value: memory.set gives a profile row no inline vector, and
the worker's reconcile only embeds a section with none.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.embedding import _cache as ec
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import profile_search as ps
from server.memory.app.loom_search.fanout import profile_hit_matches


def _row(ns: str, key: str, value: str, h: int) -> dict[str, Any]:
    return {
        "namespace": ns,
        "agent": "ui",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "value": f'"{value}"',
        "created_at": "2026-09-01 00:00:00",
        "text_hash": h,
    }


class _Embedder:
    model_name = "m"
    dim = 3

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [1.0, 0.0, 0.0]


class _CH:
    """Answers the leg's three statements; records every statement."""

    def __init__(self, rows: list[dict[str, Any]], vecs: list[dict[str, Any]]) -> None:
        self.rows, self.vecs, self.sqls = rows, vecs, []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.sqls.append(sql)
        if "SELECT DISTINCT" in sql:
            return [
                {k: r[k] for k in ("namespace", "agent", "user_id", "session_id", "memory_key")}
                for r in self.rows
            ]
        if "loom.memory_embeddings" in sql:
            return self.vecs
        return self.rows


def _vec(row: dict[str, Any], sim: float, *, dim: int = 3, h: int | None = None) -> dict:
    return {
        **{k: row[k] for k in ("namespace", "agent", "user_id", "session_id", "memory_key")},
        "scope": "private",
        "excerpt_hash": row["text_hash"] if h is None else h,
        "embedding_dim": dim,
        "similarity": sim,
    }


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_loom_search_profile_min_similarity", 0.38)
    monkeypatch.setattr(settings, "mcp_loom_search_profile_soft_similarity", 0.30)
    monkeypatch.setattr(settings, "mcp_loom_search_profile_margin", 0.08)


def _run(
    ch: _CH,
    query: str,
    emb: _Embedder | None = None,
    queued: list | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> list:
    if monkeypatch is not None:
        monkeypatch.setattr(
            ps,
            "_queue_detached",
            lambda org, rows, e: (queued if queued is not None else []).extend(rows),
        )
    return asyncio.run(
        ps.search_profile(
            "o",
            ["n"],
            query,
            top_k=5,
            lexical=profile_hit_matches,
            ch=ch,
            embedder=emb or _Embedder(),
            scope=WHOLE_NAMESPACE,
        )
    )


def test_no_profile_costs_one_small_read_and_no_embed() -> None:
    ch, emb = _CH([], []), _Embedder()
    assert _run(ch, "what do I like to drink?", emb) == []
    assert len(ch.sqls) == 1 and "FINAL" not in ch.sqls[0] and emb.calls == 0


def test_words_decide_only_for_a_section_without_a_current_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The word rule matched 15 of 30 unrelated calibration questions: with a
    current vector the gate decides alone; without one the words still do."""
    style = _row("n", "communication style", "terse replies about clickhouse", 1)
    other = _row("n", "persona", "Senior SRE", 2)
    scored = _CH([style, other], [_vec(style, 0.10), _vec(other, 0.05)])
    q = "how do I rotate the clickhouse password?"
    assert _run(scored, q, monkeypatch=monkeypatch) == []
    unscored = _CH([style, other], [])
    hits = _run(unscored, q, monkeypatch=monkeypatch)
    assert [(h["label"], h["match"]) for h in hits] == [("communication style", "lexical")]


def test_the_words_miss_and_the_meaning_finds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    tea = _row("n", "beverages", "green tea every morning", 1)
    job = _row("n", "persona", "Senior SRE", 2)
    ch = _CH([tea, job], [_vec(tea, 0.33), _vec(job, 0.12)])
    hits = _run(ch, "what do I like to drink?", monkeypatch=monkeypatch)
    # The words miss ("drink" is in neither label nor value). 0.33 is under
    # the hard floor, but the question is about the user and 0.21 over the
    # runner-up: the soft tier admits it.
    assert [(h["label"], h["match"]) for h in hits] == [("beverages", "semantic")]
    assert hits[0]["similarity"] == 0.33


def test_the_soft_tier_needs_a_question_about_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    tea = _row("n", "drinks", "green tea", 1)
    job = _row("n", "persona", "Senior SRE", 2)
    ch = _CH([tea, job], [_vec(tea, 0.33), _vec(job, 0.12)])
    assert _run(ch, "which beverage is popular in Japan", monkeypatch=monkeypatch) == []


def test_a_lone_section_needs_the_hard_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    tea = _row("n", "drinks", "green tea", 1)
    assert _run(_CH([tea], [_vec(tea, 0.33)]), "what do I like?", monkeypatch=monkeypatch) == []
    assert _run(_CH([tea], [_vec(tea, 0.40)]), "what do I like?", monkeypatch=monkeypatch)


def test_a_close_runner_up_blocks_the_soft_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _row("n", "drinks", "tea", 1), _row("n", "food", "rice", 2)
    ch = _CH([a, b], [_vec(a, 0.33), _vec(b, 0.30)])
    assert _run(ch, "what do I like?", monkeypatch=monkeypatch) == []


def test_a_stale_vector_scores_nothing_and_is_healed(monkeypatch: pytest.MonkeyPatch) -> None:
    tea = _row("n", "beverages", "black coffee now", 1)
    job = _row("n", "persona", "Senior SRE", 2)
    # The beverages vector embeds an older value (hash 99): it must not score.
    ch = _CH([tea, job], [_vec(tea, 0.9, h=99), _vec(job, 0.1)])
    queued: list = []
    assert _run(ch, "what do I like to drink?", queued=queued, monkeypatch=monkeypatch) == []
    assert [r["memory_key"] for r in queued] == ["beverages"]
    assert queued[0]["scope"] == "private"


def test_a_queued_stub_is_not_queued_again(monkeypatch: pytest.MonkeyPatch) -> None:
    tea = _row("n", "drinks", "tea", 1)
    queued: list = []
    _run(
        _CH([tea], [_vec(tea, 0.0, dim=0)]),
        "what do I like?",
        queued=queued,
        monkeypatch=monkeypatch,
    )
    assert queued == []


def test_a_missing_vector_is_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    tea = _row("n", "drinks", "tea", 1)
    queued: list = []
    _run(_CH([tea], []), "what do I like?", queued=queued, monkeypatch=monkeypatch)
    assert [r["memory_key"] for r in queued] == ["drinks"]


def test_zero_hard_floor_is_lexical_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    tea = _row("n", "drinks", "tea", 1)
    ch, emb = _CH([tea], [_vec(tea, 0.99)]), _Embedder()
    monkeypatch.setattr(settings, "mcp_loom_search_profile_min_similarity", 0.0)
    assert _run(ch, "what do I like?", emb) == [] and emb.calls == 0
    assert [h["match"] for h in _run(ch, "my drinks", emb)] == ["lexical"]


def test_the_statements_read_keys_first_and_final_only_for_them() -> None:
    assert "FINAL" not in ps.PROFILE_LOCATE_SQL
    assert "memory_type = 'profile'" in ps.PROFILE_LOCATE_SQL.split("PREWHERE", 1)[1]
    fetch = ps.PROFILE_FETCH_SQL
    assert "SELECT *" not in fetch and "FROM loom.memory FINAL" in fetch
    # Only sort-key columns in PREWHERE under FINAL.
    prewhere = fetch.split("PREWHERE", 1)[1].split("WHERE", 1)[0]
    assert "memory_type" not in prewhere and "tombstoned_at" not in prewhere
    assert "concat(memory_key, '\\n', value)" in fetch  # hashes profile_embed_text


def test_the_stub_embeds_label_and_value_where_the_old_vector_lives() -> None:
    written: list = []

    class _Repo:
        pass

    async def _insert(rows: list, ch: Any = None) -> None:
        written.extend(rows)

    import server.memory.store as mr

    orig = mr.insert_embedding_rows
    mr.insert_embedding_rows = _insert  # type: ignore[assignment]
    try:
        row = {**_row("n", "drinks", "tea", 1), "scope": "shared"}
        n = asyncio.run(ps.queue_profile_reembed("o", [row], embedder=_Embedder()))
    finally:
        mr.insert_embedding_rows = orig  # type: ignore[assignment]
    assert n == 1
    stub = written[0]
    assert stub["content_excerpt"] == 'drinks\n"tea"' == ps.profile_embed_text("drinks", '"tea"')
    assert stub["embedding_dim"] == 0 and stub["embedding"] == []  # the insert pads it
    assert stub["scope"] == "shared" and stub["embedding_model"] == "m"
    assert stub["created_at"] == "2026-09-01 00:00:00" and stub["memory_type"] == "profile"


def test_every_profile_write_queues_a_reembed(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_profile import ProfileSetInput
    from server.memory.app import profile as ph

    seen: list = []

    class _Out:
        version = 1
        memory_id = "m"
        content_sha256 = "s"
        namespace = "n"

    async def _set(inp: Any) -> _Out:
        return _Out()

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    monkeypatch.setattr(ps, "schedule_reembed_after_write", lambda org, ident: seen.append(ident))
    asyncio.run(
        ph.handle_profile_set(
            ProfileSetInput(org="o", namespace="n", agent="ui", label="drinks", value="tea")
        )
    )
    assert seen == [("n", "ui", "-", "-", "drinks")]


# ---- single-flight: one provider call per text in flight -------------------


@pytest.fixture()
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_cache", ec._LRU(max_size=100, ttl_s=60))
    monkeypatch.setattr(ec, "_INFLIGHT", {})

    async def _no_shared(*_: Any) -> None:
        return None

    monkeypatch.setattr(ec, "_shared_get", _no_shared)
    monkeypatch.setattr(ec, "_shared_put", lambda *_: None)


def test_concurrent_legs_share_one_provider_call(_fresh_cache: None) -> None:
    calls: list[str] = []

    async def fetch(text: str) -> list[float]:
        calls.append(text)
        await asyncio.sleep(0.01)
        return [0.5]

    async def legs() -> list:
        return await asyncio.gather(
            *[ec.cached_embed("m", "what do I drink", fetch, dim=1) for _ in range(4)]
        )

    out = asyncio.run(legs())
    assert len(calls) == 1
    assert [v for v, _ in out] == [[0.5]] * 4
    assert sorted(hit for _, hit in out) == [False, True, True, True]
    assert ec._INFLIGHT == {}


def test_a_failed_owners_error_reaches_its_waiters_without_another_call(
    _fresh_cache: None,
) -> None:
    """The owner's fetch already ran the provider's retries; each waiter
    calling again would repeat them per leg during an outage."""
    calls: list[int] = []

    async def fetch(text: str) -> list[float]:
        calls.append(1)
        await asyncio.sleep(0.01)
        raise RuntimeError("provider 500 after retries")

    async def legs() -> list:
        return await asyncio.gather(
            *[ec.cached_embed("m", "q", fetch, dim=1) for _ in range(3)],
            return_exceptions=True,
        )

    out = asyncio.run(legs())
    assert all(isinstance(e, RuntimeError) for e in out) and len(calls) == 1
    assert ec._INFLIGHT == {}


def test_a_cancelled_owner_does_not_cancel_its_waiters(_fresh_cache: None) -> None:
    async def fetch(text: str) -> list[float]:
        await asyncio.sleep(0.05)
        return [0.9]

    async def legs() -> tuple:
        owner = asyncio.create_task(ec.cached_embed("m", "q", fetch, dim=1))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(ec.cached_embed("m", "q", fetch, dim=1))
        await asyncio.sleep(0.01)
        owner.cancel()
        return await asyncio.gather(owner, waiter, return_exceptions=True)

    owner, waiter = asyncio.run(legs())
    assert isinstance(owner, asyncio.CancelledError)
    assert waiter == ([0.9], False)
    assert ec._INFLIGHT == {}
