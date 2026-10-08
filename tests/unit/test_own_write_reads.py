"""A point read that follows the caller's own write reads with
sequential consistency: across several web workers a plain read can miss
the write just made (working_memory.get its set, knowledge.get its add,
memory.get the update), and `append` would build its write from a stale head
and drop the text before it. Each read path below must issue its
statement inside `own_write_reads()`; the setting turns it off."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.core.ch as chmod
from server.core.ch import (
    own_write_reads,
    query_settings,
    sequential_reads,
    sequential_reads_active,
)

# ---- the scope itself -------------------------------------------------------


def test_the_scope_adds_sequential_consistency_to_every_statement() -> None:
    assert "select_sequential_consistency" not in query_settings()
    with own_write_reads():
        assert sequential_reads_active()
        assert query_settings()["select_sequential_consistency"] == 1
    assert not sequential_reads_active()


def test_an_outer_scope_survives() -> None:
    with sequential_reads():
        with own_write_reads():
            assert sequential_reads_active()
        assert sequential_reads_active(), "the inner scope must not reset the outer one"


# ---- a recording client -----------------------------------------------------


class _RecCH:
    """Records, per statement, whether it ran inside a sequential scope."""

    def __init__(self, one: Any = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.seen: list[bool] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self._one = one
        self._rows = rows or []

    async def query_one(self, sql: str, params: Any = None) -> Any:
        self.seen.append(sequential_reads_active())
        return self._one

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.seen.append(sequential_reads_active())
        return list(self._rows)

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


def _wm_input(**kw: Any) -> SimpleNamespace:
    base = dict(
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        run_id="-",
        block_key="b",
        block_keys=[],
        limit=20,
        format="xml",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---- working memory: the head read and render --------------------------------


def test_working_memory_head_and_render_read_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.working_memory as wm

    head = {
        "block_key": "b",
        "content": "first",
        "char_limit": 4096,
        "version": 1,
        "updated_at": None,
    }
    ch = _RecCH(one=head, rows=[head])
    monkeypatch.setattr(wm, "get_client", lambda: ch)

    asyncio.run(wm._get_row(_wm_input(), "b"))
    asyncio.run(wm.handle_working_memory_render(_wm_input()))
    assert ch.seen == [True, True]


def test_append_builds_its_write_from_the_consistent_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """The head `append` reads is the consistent one, and the write keeps the
    text before the addition (a stale miss rewrote the block as the addition
    alone)."""
    import server.memory.app.working_memory as wm
    from server.memory.api.models_working_memory import WorkingMemoryAppendInput

    head = {
        "block_key": "b",
        "content": "first",
        "char_limit": 4096,
        "version": 1,
        "updated_at": None,
    }

    class _Lagging(_RecCH):
        async def query_one(self, sql: str, params: Any = None) -> Any:
            self.seen.append(sequential_reads_active())
            return head if sequential_reads_active() else None  # a lagging replica

    ch = _Lagging()
    monkeypatch.setattr(wm, "get_client", lambda: ch)
    asyncio.run(
        wm.handle_working_memory_append(
            WorkingMemoryAppendInput(
                org="o", namespace="n", agent="a", block_key="b", addition="second"
            )
        )
    )
    written = ch.inserts[-1][1][0]["content"]
    assert written == "first\nsecond", written


# ---- knowledge, memory, profile ---------------------------------------------


def test_knowledge_get_list_outline_read_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.knowledge.app.document_reads as kn
    from server.knowledge.api.models_knowledge import KnowledgeGetInput, KnowledgeListInput
    from server.knowledge.store import documents as documents_repo

    seen: list[bool] = []

    async def _get_one(*_a: Any, **_k: Any) -> None:
        seen.append(sequential_reads_active())
        return None

    async def _list(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        seen.append(sequential_reads_active())
        return []

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "list_with_chunk_counts", _list)
    asyncio.run(kn.handle_knowledge_get(KnowledgeGetInput(org="o", namespace="n", source_uri="u")))
    asyncio.run(kn.handle_knowledge_list(KnowledgeListInput(org="o", namespace="n")))
    from server.knowledge.api.models_knowledge import KnowledgeOutlineInput

    asyncio.run(
        kn.handle_knowledge_outline(KnowledgeOutlineInput(org="o", namespace="n", source_uri="u"))
    )
    assert seen == [True, True, True]


def test_memory_get_and_list_read_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.handlers as mh
    import server.memory.app.query as mq
    from server.memory.api.models_crud import (
        MemoryGetInput,
        MemoryGetOutput,
        MemoryListInput,
        MemoryListOutput,
    )

    seen: list[bool] = []

    async def _get(inp: Any) -> Any:
        seen.append(sequential_reads_active())
        return MemoryGetOutput(found=False)

    async def _list(inp: Any) -> Any:
        seen.append(sequential_reads_active())
        return MemoryListOutput(entries=[], truncated=False)

    monkeypatch.setattr(mh, "_handle_memory_get", _get)
    monkeypatch.setattr(mq, "_handle_memory_list", _list)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a, **k: None)
    monkeypatch.setattr(mq, "_cap_viewer_list_limit", lambda i: i)
    asyncio.run(
        mh.handle_memory_get(MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k"))
    )
    asyncio.run(mq.handle_memory_list(MemoryListInput(org="o", namespace="n", agent="a")))
    assert seen == [True, True]


def test_profile_render_reads_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.profile as ph
    from server.memory.api.models_profile import ProfileRenderInput

    ch = _RecCH(rows=[])
    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    asyncio.run(ph.handle_profile_render(ProfileRenderInput(org="o", namespace="n", agent="a")))
    assert ch.seen and all(ch.seen)


# ---- the profile leg ----------------------------------------------------------


class _Emb:
    model_name = "fake"

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.batches: list[list[str]] = []

    async def embed(self, text: str) -> list[float]:
        return self.table[text]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [self.table[t] for t in texts]


def test_a_section_written_a_moment_ago_is_scored_from_its_own_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-embed is queued detached, so a section appended a second ago
    has no current vector; without on-the-fly scoring it would fall back to
    the word rule ('likes tea' never matches 'what do I like to drink?'). Its
    own text is embedded in one batch and scored; the heal is still queued."""
    import server.memory.app.profile_search as ps

    q = "what do I like to drink?"
    row = {
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "drinks",
        "value": '"likes tea"',
        "text_hash": 7,
    }
    emb = _Emb({q: [1.0, 0.0], ps.profile_embed_text("drinks", '"likes tea"'): [0.8, 0.6]})
    queued: list[Any] = []
    monkeypatch.setattr(ps, "_queue_detached", lambda org, rows, e: queued.append(rows))

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []  # no vector row yet

    sims = asyncio.run(ps._similarities(_CH(), "o", [row], q, embedder=emb))
    assert sims == {ps._ident(row): pytest.approx(0.8)}
    assert emb.batches == [[ps.profile_embed_text("drinks", '"likes tea"')]]
    assert queued, "the stale section is still queued for its stored re-embed"


def test_a_current_vector_is_not_re_embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.profile_search as ps

    row = {
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "drinks",
        "value": '"likes tea"',
        "text_hash": 7,
    }
    emb = _Emb({"q": [1.0, 0.0]})

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [
                {
                    **{
                        k: row[k]
                        for k in ("namespace", "agent", "user_id", "session_id", "memory_key")
                    },
                    "scope": "private",
                    "excerpt_hash": 7,
                    "embedding_dim": 2,
                    "similarity": 0.9,
                }
            ]

    sims = asyncio.run(ps._similarities(_CH(), "o", [row], "q", embedder=emb))
    assert sims == {ps._ident(row): 0.9} and emb.batches == []


def test_the_leg_reads_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.profile_search as ps

    ch = _RecCH(rows=[])
    out = asyncio.run(ps.search_profile("o", ["n"], "q", top_k=3, lexical=lambda *a: False, ch=ch))
    assert out == [] and ch.seen == [True]
