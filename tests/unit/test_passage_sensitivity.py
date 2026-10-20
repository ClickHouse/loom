"""A raw passage that reads as a credential is labelled 'high' when written, never sent to
the embedder, and held back from the reads that serve passages to every reader."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.domain.search_types import SearchHit
from server.memory.domain.sensitivity import passage_reads_high, passage_sensitivity

# Split so the repository's secret scan does not read the fixtures as keys.
_OPENAI_KEY = "sk-" + "proj-" + "Ab3" * 12
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_PLAIN = "We moved the standup to Thursday mornings and the deploy plan is unchanged."


@pytest.mark.parametrize(
    "text",
    [
        f"[user] my key is {_OPENAI_KEY}, can you check it?",
        f"[user] the staging account uses {_AWS_KEY_ID} for uploads",
        # A chunk of a JSON value: the token follows an escaped newline.
        '{"note": "rotate it\\npassword=' + "hunter2hunter2" + '"}',
    ],
)
def test_a_passage_holding_a_credential_is_labelled_high(text: str) -> None:
    assert passage_sensitivity(text) == "high"


def test_a_plain_passage_and_a_contact_detail_are_unlabelled() -> None:
    assert passage_sensitivity(_PLAIN) == ""
    # A passage has no medium tier: only 'high' changes what is embedded and served.
    assert passage_sensitivity("mail me at someone@example.com") == ""


def test_the_label_ignores_the_autodetect_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", False, raising=False)
    assert passage_sensitivity(f"key {_OPENAI_KEY}") == "high"


def test_a_passage_is_held_back_by_its_label_or_by_its_text() -> None:
    assert passage_reads_high("high", _PLAIN)
    assert passage_reads_high("", f"key {_AWS_KEY_ID}")  # written before the label existed
    assert passage_reads_high(None, f"key {_AWS_KEY_ID}")
    assert not passage_reads_high("", _PLAIN)


@pytest.mark.asyncio
async def test_embed_plain_never_sends_a_high_text_to_the_embedder() -> None:
    from server.memory.app.chunk_projection import embed_plain

    sent: list[list[str]] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        sent.append(list(texts))
        return [[float(i + 1)] for i in range(len(texts))]

    texts = ["plain one", f"key {_OPENAI_KEY}", "plain two"]
    vectors = await embed_plain(texts, [passage_sensitivity(t) for t in texts], embed)
    assert sent == [["plain one", "plain two"]]
    assert vectors == [[1.0], [], [2.0]]


@pytest.mark.asyncio
async def test_embed_plain_makes_no_call_when_every_text_is_high() -> None:
    from server.memory.app.chunk_projection import embed_plain

    async def embed(texts: list[str]) -> list[list[float]]:
        raise AssertionError("no text may reach the embedder")

    assert await embed_plain([f"key {_OPENAI_KEY}"], ["high"], embed) == [[]]


class _Embedder:
    model_name = "fake"
    dim = 2

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return [[0.6, 0.8] for _ in texts]

    async def embed(self, text: str) -> list[float]:
        self.seen.append(text)
        return [0.6, 0.8]


class _CH:
    def __init__(self) -> None:
        self.inserts: dict[str, list[dict[str, Any]]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.setdefault(table, []).extend(rows)

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_a_conversation_chunk_holding_a_key_is_stored_labelled_and_unembedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as chmod
    import server.embedding as embmod
    from server.memory.app.chunk_projection import write_memory_chunks

    ch, emb = _CH(), _Embedder()
    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    monkeypatch.setattr(embmod, "get_embedder", lambda: emb)

    class _Input:
        org = "o"
        namespace = "n"
        agent = "a"
        user_id = "u"
        session_id = "s"
        run_id = "-"

    # Two paragraphs over the chunk size, so each lands in its own chunk.
    secret_turn = f"here is the key {_OPENAI_KEY} " + "and some context. " * 40
    plain_turn = _PLAIN + " " + "More notes on the plan. " * 40
    await write_memory_chunks(
        _Input(),
        [{"role": "user", "content": plain_turn}, {"role": "user", "content": secret_turn}],
    )
    rows = ch.inserts["loom.memory_chunks"]
    high = [r for r in rows if _OPENAI_KEY in r["content"]]
    plain = [r for r in rows if _OPENAI_KEY not in r["content"]]
    assert high and plain
    for r in high:
        assert (r["sensitivity"], r["embedding"], r["embedding_dim"]) == ("high", [], 0)
        assert r["content"] == r["content"].strip() and "[user]" in r["content"]
    for r in plain:
        assert (r["sensitivity"], r["embedding_dim"]) == ("", 2)
    assert not any(_OPENAI_KEY in t for t in emb.seen)


@pytest.mark.asyncio
async def test_a_long_value_chunk_holding_a_key_is_stored_labelled_and_unembedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.memory.app import set_chunks

    async def head_still_embedded(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(set_chunks, "_head_still_embedded", head_still_embedded)
    ch, emb = _CH(), _Embedder()
    value = ("Plain paragraph about the release. " * 30) + "\n\n" + (f"key {_AWS_KEY_ID} " * 30)
    write = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="u", memory_key="notes/release", value=value
    )
    written = await set_chunks.write_set_chunks(
        write, memory_type="semantic", limit=100, ch=ch, embedder=emb
    )
    rows = ch.inserts["loom.memory_chunks"]
    assert written == len(rows)
    levels = {r["sensitivity"] for r in rows}
    assert levels == {"", "high"}
    for r in rows:
        assert (r["embedding_dim"] == 0) == (r["sensitivity"] == "high")
    assert not any(_AWS_KEY_ID in t for t in emb.seen)


def test_a_tombstone_carries_the_chunk_label_forward() -> None:
    from server.memory.app.set_chunks import _chunk_state_row

    row = _chunk_state_row(
        {"chunk_index": 0, "content_sha256": "x" * 64, "sensitivity": "high"},
        org="o",
        namespace="n",
    )
    assert row["sensitivity"] == "high"


def _hit(key: str, excerpt: str, rank: float) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="episodic",
        content_excerpt=excerpt,
        distance=1.0 - rank,
        rank_score=rank,
        temporal_anchor="",
        source="chunk",
    )


def test_the_chunk_leg_serves_no_chunk_that_reads_as_a_credential() -> None:
    from server.memory.app.retrieval.chunks import _served_chunks

    hits = [
        _hit("chunk:s:0", "[user] here is the", 0.9),  # the excerpt is trimmed; the text is not
        _hit("chunk:s:1", _PLAIN, 0.8),
        _hit("chunk:s:2", _PLAIN, 0.7),
    ]
    rows = [
        {"memory_key": "chunk:s:0", "content_full": f"[user] here is the key {_OPENAI_KEY}"},
        {"memory_key": "chunk:s:1", "content_full": _PLAIN},
        {"memory_key": "chunk:s:2", "content_full": _PLAIN},
    ]
    assert [h.memory_key for h in _served_chunks(hits, rows, 5)] == ["chunk:s:1", "chunk:s:2"]
    # A held-back chunk does not take one of the `n` places.
    assert [h.memory_key for h in _served_chunks(hits, rows, 1)] == ["chunk:s:1"]


@pytest.mark.asyncio
async def test_the_summary_leg_serves_no_digest_that_reads_as_a_credential() -> None:
    from server.memory.app.retrieval.summaries import fetch_session_summaries

    class _Rows:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [
                {
                    "session_id": "s1",
                    "content_excerpt": f"The user shared {_AWS_KEY_ID}.",
                    "distance": 0.1,
                },
                {"session_id": "s2", "content_excerpt": _PLAIN, "distance": 0.2},
            ]

    hits = await fetch_session_summaries(
        _Rows(),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        query_vec=[0.6, 0.8],
        embedding_model="fake",
    )
    assert [h.memory_key for h in hits] == ["summary:s2"]


@pytest.mark.asyncio
async def test_the_ask_chunk_lookups_serve_no_chunk_that_reads_as_a_credential() -> None:
    from server.memory.store.lookups import find_chunks_by_identifier, find_chunks_by_session_date

    class _Rows:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [
                {"memory_key": "chunk:s:0", "content_excerpt": f"deploy key {_OPENAI_KEY}"},
                {"memory_key": "chunk:s:1", "content_excerpt": f"deploy {_PLAIN}"},
            ]

    by_id = await find_chunks_by_identifier(
        org="o", namespace="n", identifiers=["deploy"], ch=_Rows()
    )
    by_date = await find_chunks_by_session_date(
        org="o", namespace="n", start_date="2026-01-01", end_date="2026-01-02", ch=_Rows()
    )
    assert [r["memory_key"] for r in by_id] == ["chunk:s:1"]
    assert [r["memory_key"] for r in by_date] == ["chunk:s:1"]


@pytest.mark.asyncio
async def test_a_digest_that_reads_as_a_credential_is_drafted_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import server.llm.client as llm_client
    from server.memory.app import session_summary

    class _LLM:
        async def complete(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(
                text=f"The user pasted {_OPENAI_KEY}.", tokens_in=1, tokens_out=1
            )

    emb = _Embedder()
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    monkeypatch.setattr(session_summary, "get_embedder", lambda: emb)
    draft = await session_summary._draft_session_summary(
        SimpleNamespace(session_id="s"), [{"role": "user", "content": "x"}], None
    )
    assert draft is not None
    assert draft.embedding == [] and emb.seen == []
    assert _OPENAI_KEY in draft.summary_text  # stored as written
