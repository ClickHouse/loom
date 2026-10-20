"""A raw passage whose text reads as a credential is stored as written with no vector, is
never sent to the embedder, and is held back from the reads that serve passages to every
reader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain.search_types import SearchHit

# Split so the repository's secret scan does not read the fixtures as keys.
_OPENAI_KEY = "sk-" + "proj-" + "Ab3" * 12
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_PLAIN = "We moved the standup to Thursday mornings and the deploy plan is unchanged."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        f"my key is {_OPENAI_KEY}, can you check it?",
        f"the staging account uses {_AWS_KEY_ID} for uploads",
        # A chunk of a JSON value: the token follows an escaped newline.
        '{"note": "rotate it\\npassword=' + "hunter2hunter2" + '"}',
    ],
)
async def test_embed_plain_never_sends_a_credential_to_the_embedder(secret: str) -> None:
    from server.memory.app.credential_passages import embed_plain

    sent: list[list[str]] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        sent.append(list(texts))
        return [[float(i + 1)] for i in range(len(texts))] if texts[0] != "fails" else [[]]

    out = await embed_plain(["plain one", secret, "plain two"], embed)
    assert [(e.vector, e.held, e.written) for e in out] == [
        ([1.0], False, True),
        ([], True, True),
        ([2.0], False, True),
    ]
    assert sent == [["plain one", "plain two"]]
    # A failed embed is not held: the caller drops it rather than storing it unembedded.
    [failed] = await embed_plain(["fails"], embed)
    assert (failed.vector, failed.held, failed.written) == ([], False, False)


@pytest.mark.asyncio
async def test_embed_plain_makes_no_call_when_every_text_reads_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.credential_passages import embed_plain
    from server.settings import settings

    # The text decides, whatever the autodetect setting says.
    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", False)

    async def embed(texts: list[str]) -> list[list[float]]:
        raise AssertionError("no text may reach the embedder")

    [e] = await embed_plain([f"key {_OPENAI_KEY}"], embed)
    assert (e.vector, e.held) == ([], True)


def test_without_credentials_keeps_order_and_fills_the_limit() -> None:
    from server.memory.domain.sensitivity import without_credentials

    items = [f"key {_AWS_KEY_ID}", "one", f"key {_OPENAI_KEY}", "two", "three"]
    assert without_credentials(items, str) == ["one", "two", "three"]
    assert without_credentials(items, str, 2) == ["one", "two"]
    assert without_credentials([], str, 2) == []


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
async def test_a_conversation_chunk_holding_a_key_is_stored_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as chmod
    import server.embedding as embmod
    from server.memory.app.chunk_projection import write_memory_chunks

    ch, emb = _CH(), _Embedder()
    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    monkeypatch.setattr(embmod, "get_embedder", lambda: emb)
    write = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="u", session_id="s", run_id="-"
    )
    # Two turns over the chunk size, so each lands in its own chunk.
    secret_turn = f"here is the key {_OPENAI_KEY} " + "and some context. " * 40
    plain_turn = _PLAIN + " " + "More notes on the plan. " * 40
    await write_memory_chunks(
        write, [{"role": "user", "content": plain_turn}, {"role": "user", "content": secret_turn}]
    )
    rows = ch.inserts["loom.memory_chunks"]
    held = [r for r in rows if _OPENAI_KEY in r["content"]]
    plain = [r for r in rows if _OPENAI_KEY not in r["content"]]
    assert held and plain
    assert all((r["embedding"], r["embedding_dim"]) == ([], 0) for r in held)
    assert all(r["embedding_dim"] == 2 for r in plain)
    assert not any(_OPENAI_KEY in t for t in emb.seen)


@pytest.mark.asyncio
async def test_a_long_value_chunk_holding_a_key_is_stored_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert {r["embedding_dim"] == 0 for r in rows} == {True, False}
    for r in rows:
        assert (r["embedding_dim"] == 0) == (_AWS_KEY_ID in r["content"])
    assert not any(_AWS_KEY_ID in t for t in emb.seen)


@pytest.mark.asyncio
async def test_import_evidence_stores_a_credential_chunk_and_digest_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.embedding as embmod
    from server.memory.api.models_evidence_transfer import (
        EvidenceChunk,
        EvidenceDigest,
        MemoryImportEvidenceInput,
    )
    from server.memory.app import evidence_transfer

    async def not_refused(*_: Any, **__: Any) -> bool:
        return False

    ch, emb = _CH(), _Embedder()
    monkeypatch.setattr(evidence_transfer, "get_client", lambda: ch)
    monkeypatch.setattr(embmod, "get_embedder", lambda: emb)
    monkeypatch.setattr(evidence_transfer, "_refused", not_refused)
    monkeypatch.setattr(evidence_transfer, "_importer_user", lambda _input: None)
    out = await evidence_transfer.handle_memory_import_evidence(
        MemoryImportEvidenceInput(
            org="o",
            namespace="n",
            agent="a",
            chunks=[
                EvidenceChunk(session_id="s", chunk_index=0, content=_PLAIN),
                EvidenceChunk(session_id="s", chunk_index=1, content=f"key {_OPENAI_KEY}"),
                EvidenceChunk(session_id="s", chunk_index=2, content="   "),
            ],
            digests=[EvidenceDigest(session_id="s", summary_text=f"Shared {_AWS_KEY_ID}.")],
        )
    )
    chunks = {r["chunk_index"]: r for r in ch.inserts["loom.memory_chunks"]}
    assert set(chunks) == {0, 1}
    assert (chunks[0]["embedding_dim"], chunks[1]["embedding_dim"]) == (2, 0)
    assert chunks[1]["content"] == f"key {_OPENAI_KEY}"  # stored as written
    [digest] = ch.inserts["loom.session_summaries"]
    assert digest["embedding_dim"] == 0 and digest["summary_text"] == f"Shared {_AWS_KEY_ID}."
    assert (out.chunks_written, out.digests_written, out.skipped) == (2, 1, 1)
    assert out.embedding_dim == 2
    assert emb.seen == [_PLAIN]


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
    from server.memory.app.retrieval.chunks import _served

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
    assert [h.memory_key for h in _served(hits, rows, 5)] == ["chunk:s:1", "chunk:s:2"]
    # A held-back chunk does not take one of the `n` places.
    assert [h.memory_key for h in _served(hits, rows, 1)] == ["chunk:s:1"]


@pytest.mark.asyncio
async def test_the_summary_leg_serves_no_digest_that_reads_as_a_credential() -> None:
    from server.memory.app.retrieval.summaries import fetch_session_summaries

    class _Rows:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [
                {
                    # The key straddles the served excerpt's 1200-character cut.
                    "session_id": "s1",
                    "summary_text": "x" * 1195 + f" {_AWS_KEY_ID}.",
                    "distance": 0.1,
                },
                {
                    "session_id": "s2",
                    "summary_text": _PLAIN,
                    "distance": 0.2,
                },
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
    from server.memory.app.ask.identifiers import _plain_chunks
    from server.memory.domain.sensitivity import HELD_MARGIN as _HELD_MARGIN

    asked: list[int] = []

    async def find(*, limit: int, **_: Any) -> list[dict[str, Any]]:
        asked.append(limit)
        return [
            {"memory_key": "chunk:s:0", "content_excerpt": f"deploy key {_OPENAI_KEY}"},
            {"memory_key": "chunk:s:1", "content_excerpt": f"deploy {_PLAIN}"},
            {"memory_key": "chunk:s:2", "content_excerpt": "deploy notes"},
        ]

    rows = await _plain_chunks(find, limit=2, org="o")
    assert [r["memory_key"] for r in rows] == ["chunk:s:1", "chunk:s:2"]
    assert asked == [2 + _HELD_MARGIN]


@pytest.mark.asyncio
async def test_a_digest_that_reads_as_a_credential_is_drafted_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


@pytest.mark.asyncio
async def test_embed_plain_refuses_a_short_answer_from_the_embedder() -> None:
    from server.memory.app.credential_passages import embed_plain

    async def short(texts: list[str]) -> list[list[float]]:
        return [[1.0]]  # one vector for two texts

    with pytest.raises(ValueError, match="embedder returned 1 vectors for 2 texts"):
        await embed_plain(["plain one", f"key {_OPENAI_KEY}", "plain two"], short)
