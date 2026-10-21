"""A raw passage whose text reads as a credential is stored as written with no vector, is
never sent to the embedder, and is held back from the reads that serve passages to every
reader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain.search_types import SearchHit
from tests.unit._fake_credentials import (
    AWS_KEY_ID,
    OPENAI_KEY,
    PRIVATE_KEY_BLOCK,
    PRIVATE_KEY_LINES,
)

_PLAIN = "We moved the standup to Thursday mornings and the deploy plan is unchanged."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        f"my key is {OPENAI_KEY}, can you check it?",
        f"the staging account uses {AWS_KEY_ID} for uploads",
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

    [e] = await embed_plain([f"key {OPENAI_KEY}"], embed)
    assert (e.vector, e.held) == ([], True)


def test_without_credentials_keeps_order_and_fills_the_limit() -> None:
    from server.memory.domain.sensitivity import without_credentials

    items = [f"key {AWS_KEY_ID}", "one", f"key {OPENAI_KEY}", "two", "three"]
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
    secret_turn = f"here is the key {OPENAI_KEY} " + "and some context. " * 40
    plain_turn = _PLAIN + " " + "More notes on the plan. " * 40
    await write_memory_chunks(
        write, [{"role": "user", "content": plain_turn}, {"role": "user", "content": secret_turn}]
    )
    rows = ch.inserts["loom.memory_chunks"]
    held = [r for r in rows if OPENAI_KEY in r["content"]]
    plain = [r for r in rows if OPENAI_KEY not in r["content"]]
    assert held and plain
    assert all((r["embedding"], r["embedding_dim"]) == ([], 0) for r in held)
    assert all(r["embedding_dim"] == 2 for r in plain)
    assert not any(OPENAI_KEY in t for t in emb.seen)


@pytest.mark.asyncio
async def test_a_long_value_chunk_holding_a_key_is_stored_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import set_chunks

    async def head_still_embedded(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(set_chunks, "_head_still_embedded", head_still_embedded)
    ch, emb = _CH(), _Embedder()
    value = ("Plain paragraph about the release. " * 30) + "\n\n" + (f"key {AWS_KEY_ID} " * 30)
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
        assert (r["embedding_dim"] == 0) == (AWS_KEY_ID in r["content"])
    assert not any(AWS_KEY_ID in t for t in emb.seen)


@pytest.mark.asyncio
async def test_a_chunk_holding_only_part_of_a_key_is_stored_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import set_chunks

    async def head_still_embedded(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(set_chunks, "_head_still_embedded", head_still_embedded)
    key = OPENAI_KEY + "Zq9" * 44

    def filler(n: int) -> str:
        return ("plan notes " * 200)[: n - 1] + " "

    # No line or sentence break, so chunks are cut at 800 characters and overlap by 100:
    # [0, 800), [700, 1500), [1400, 2200). The first key crosses the first chunk's end, so
    # that chunk ends with its head; the second crosses the third chunk's start, so that
    # chunk opens with its tail. Neither fragment reads as a credential on its own.
    value = filler(780) + key + " " + filler(1300 - 781 - len(key)) + key + " " + filler(1000)
    assert (value.find(key), value.rfind(key)) == (780, 1300)
    ch, emb = _CH(), _Embedder()
    write = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="u", memory_key="notes/export", value=value
    )
    await set_chunks.write_set_chunks(write, memory_type="semantic", limit=100, ch=ch, embedder=emb)
    rows = ch.inserts["loom.memory_chunks"]
    assert [r["embedding_dim"] for r in rows] == [0, 0, 0, 2]
    fragments = {key[i : i + 8] for i in range(len(key) - 7)}
    assert emb.seen and not any(f in t for t in emb.seen for f in fragments)


@pytest.mark.asyncio
async def test_a_chunk_that_starts_inside_a_word_and_reads_as_a_key_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import set_chunks
    from server.memory.domain.sensitivity import reads_high

    async def head_still_embedded(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(set_chunks, "_head_still_embedded", head_still_embedded)

    def filler(n: int) -> str:
        return ("plan notes " * 200)[: n - 1] + " "

    # Chunks are cut at [0, 800) and [700, 1500): the second starts at the `sk-` inside
    # `risk-`, which reads as a key there although the whole text does not.
    value = filler(698) + "risk-assessment-for-the-migration-plan " + filler(800)
    assert value[700:].startswith("sk-assessment") and not reads_high(value)
    ch, emb = _CH(), _Embedder()
    write = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="u", memory_key="notes/risk", value=value
    )
    await set_chunks.write_set_chunks(write, memory_type="semantic", limit=100, ch=ch, embedder=emb)
    rows = ch.inserts["loom.memory_chunks"]
    held = [r for r in rows if r["content"].startswith("sk-assessment")]
    assert len(held) == 1 and held[0]["embedding_dim"] == 0
    assert all(r["embedding_dim"] == 2 for r in rows if r not in held)
    assert emb.seen and not any(t.startswith("sk-assessment") for t in emb.seen)


@pytest.mark.asyncio
async def test_memory_get_leaves_a_vectorless_neighbour_out_and_audits_its_own_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk held for part of a key does not read as a credential: its missing vector
    is what marks it."""
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app import handlers
    from server.observability import audit

    tail = "Zq9" * 30 + " for the export job"
    rows = [
        {"chunk_index": 0, "content": _PLAIN, "parent_memory_key": "", "embedding_dim": 2},
        {"chunk_index": 1, "content": tail, "parent_memory_key": "", "embedding_dim": 0},
    ]

    class _Chunks:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return rows

    audits: list[str] = []

    async def emit(event: str, **_: Any) -> None:
        audits.append(event)

    monkeypatch.setattr(handlers, "get_client", lambda: _Chunks())
    monkeypatch.setattr(audit, "emit", emit)

    def read(key: str) -> Any:
        return handlers._get_chunk_with_neighbors(
            MemoryGetInput(org="o", namespace="n", agent="a", memory_key=key)
        )

    plain = await read("chunk:s:0")
    assert plain.value["content"] == _PLAIN and plain.value["neighbor_count"] == 0
    assert audits == []
    held = await read("chunk:s:1")
    assert tail in held.value["content"]
    assert audits == ["memory.sensitive_read"]


def test_credential_spans_map_an_escaped_match_back_to_the_text() -> None:
    from server.memory.domain.sensitivity import credential_spans

    text = '{"note": "rotate it\\npassword=hunter2hunter2"}'
    [(start, end)] = credential_spans(text)
    assert text[start:end] == "password=hunter2hunter2"


_KEY_HEAD = "the staging account uses " + AWS_KEY_ID[:10]
_KEY_TAIL = AWS_KEY_ID[10:] + " for uploads"


def test_split_credentials_joins_only_neighbouring_chunks_of_one_session() -> None:
    from server.memory.domain.sensitivity import split_credentials

    # A gap in chunk_index: the two chunks were never neighbours, so no key is rebuilt.
    assert split_credentials([("s", 0, _KEY_HEAD), ("s", 2, _KEY_TAIL)]) == [False, False]
    # Neighbours: the key cut at the boundary reads as one when joined.
    assert split_credentials([("s", 0, _KEY_HEAD), ("s", 1, _KEY_TAIL)]) == [True, True]
    # Chunks of another session that share an index are never joined with this one's.
    assert split_credentials([("a", 0, _KEY_HEAD), ("b", 1, _KEY_TAIL), ("b", 0, _KEY_HEAD)]) == [
        False,
        True,
        True,
    ]


def test_split_credentials_holds_a_session_with_two_texts_at_one_index() -> None:
    from server.memory.domain.sensitivity import split_credentials

    # Two writes of one session each number their chunks from 0, so index 0 holds two
    # texts and there is no single text to rebuild: every chunk of the session is held.
    plain = "the build passed on the second try"
    assert split_credentials([("s", 0, plain), ("s", 0, _KEY_HEAD), ("s", 1, _KEY_TAIL)]) == [
        True,
        True,
        True,
    ]
    # The same text sent twice is one text; another session in the call is read as usual.
    chunks = [("s", 0, plain), ("s", 0, plain), ("s", 1, "The deploy is on Thursday.")]
    chunks += [("t", 0, "x"), ("t", 0, "y"), ("u", 0, _KEY_HEAD), ("u", 1, _KEY_TAIL)]
    assert split_credentials(chunks) == [False, False, False, True, True, True, True]


def test_split_credentials_keeps_a_boundary_character_that_only_happens_to_repeat() -> None:
    from server.memory.domain.sensitivity import split_credentials

    # Imported chunks with no overlap: the head ends in "X" and the tail starts with one.
    key = "AKIA" + "IOSFOX" + "XN7EXAMPLE"
    head, tail = "the staging account uses " + key[:10], key[10:] + " for uploads"
    assert split_credentials([("s", 0, head), ("s", 1, tail)]) == [True, True]


def test_split_credentials_reads_each_session_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.domain import sensitivity

    # Two sessions of 128 chunks each: the patterns run once over each session's text.
    chunks = [(s, n, f"{s} {n} notes on the plan. ") for s in ("a", "b") for n in range(128)]
    scanned: list[str] = []
    spans = sensitivity.credential_spans

    def counting(text: str) -> list[tuple[int, int]]:
        scanned.append(text)
        return spans(text)

    monkeypatch.setattr(sensitivity, "credential_spans", counting)
    assert sensitivity.split_credentials(chunks) == [False] * 256
    assert scanned == ["".join(c for s, _, c in chunks if s == name) for name in ("a", "b")]


def test_split_credentials_finds_a_key_cut_at_the_boundary_of_long_chunks() -> None:
    from server.memory.domain.sensitivity import split_credentials

    head, tail = "plain words. " * 400 + _KEY_HEAD, _KEY_TAIL + " plain words." * 400
    assert split_credentials([("s", 0, head), ("s", 1, tail)]) == [True, True]


def test_split_credentials_finds_a_key_spread_over_three_short_chunks() -> None:
    from server.memory.domain.sensitivity import split_credentials

    # Imported chunks have no minimum length, so a key can run through a whole chunk.
    parts = ["note " + AWS_KEY_ID[:4], AWS_KEY_ID[4:12], AWS_KEY_ID[12:] + " end"]
    chunks = [("s", i, part) for i, part in enumerate(parts)]
    assert split_credentials([*chunks, ("s", 3, "The build passed.")]) == [True] * 3 + [False]


@pytest.mark.asyncio
async def test_okf_import_sends_a_session_whole_so_a_key_cut_between_its_chunks_is_held() -> None:
    import argparse

    from server.cli.okf import _import_evidence
    from server.memory.domain.okf_evidence import ChunkRecord
    from server.memory.domain.sensitivity import split_credentials

    # Session "b" starts at the bundle's 41st chunk, so its chunks 23 and 24 are the bundle's
    # 64th and 65th: a cut every 64 chunks would send the two halves of the key apart.
    chunks = [ChunkRecord("a", n, f"a {n} notes on the plan. ") for n in range(40)]
    parts = {23: _KEY_HEAD, 24: _KEY_TAIL}
    chunks += [ChunkRecord("b", n, parts.get(n, f"b {n} notes on the plan. ")) for n in range(40)]
    held: dict[tuple[str, int], bool] = {}

    class Loom:
        async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
            sent = [(c["session_id"], c["chunk_index"], c["content"]) for c in body["chunks"]]
            for (session, index, _), hit in zip(sent, split_credentials(sent), strict=True):
                held[(session, index)] = hit
            return {"chunks_written": len(sent)}

    args = argparse.Namespace(namespace="ns", agent="importer", user_id="-")
    totals = await _import_evidence(Loom(), "org", args, chunks, [])  # type: ignore[arg-type]
    assert totals["chunks_written"] == 80
    assert [key for key, hit in held.items() if hit] == [("b", 23), ("b", 24)]


@pytest.mark.asyncio
async def test_every_chunk_a_private_key_block_covers_is_held_on_write() -> None:
    from server.memory.app.chunk_projection import chunk_text_with_ranges
    from server.memory.app.credential_passages import embed_plain
    from server.memory.domain.sensitivity import overlaps_credential, reads_high

    # The key's body runs past a chunk: only the first chunk holds its header.
    text = "We rotated the deploy key.\n\n" + PRIVATE_KEY_BLOCK + "\n\nThe build passed."
    chunks, ranges, _ = chunk_text_with_ranges(text, max_chunks=64)
    contents = [c for c, _, _ in chunks]
    assert len(contents) == 3 and not reads_high(contents[1])
    seen: list[str] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return [[0.6, 0.8] for _ in texts]

    out = await embed_plain(contents, embed, held=overlaps_credential(text, ranges))
    assert [e.held for e in out] == [True, True, True]
    assert not any(line in t for t in seen for line in PRIVATE_KEY_LINES)


def test_split_credentials_holds_every_chunk_of_a_private_key_block() -> None:
    from server.memory.domain.sensitivity import split_credentials

    # Imported chunks cut at line breaks: the header, the body, then the rest and the
    # closing line. The chunk after the closing line is plain.
    lines = PRIVATE_KEY_BLOCK.split("\n")
    parts = ["\n".join(lines[:12]), "\n".join(lines[12:24]), "\n".join(lines[24:])]
    chunks = [("s", i, part) for i, part in enumerate(parts)]
    assert split_credentials([*chunks, ("s", 3, "The build passed.")]) == [True] * 3 + [False]
    # A header cut at a boundary opens the block all the same.
    cut = [PRIVATE_KEY_BLOCK[:20], PRIVATE_KEY_BLOCK[20:900], PRIVATE_KEY_BLOCK[900:]]
    chunks = [("s", i, part) for i, part in enumerate(cut)]
    assert split_credentials([*chunks, ("s", 3, "The build passed.")]) == [True] * 3 + [False]


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
                EvidenceChunk(session_id="s", chunk_index=1, content=f"key {OPENAI_KEY}"),
                EvidenceChunk(session_id="s", chunk_index=2, content="   "),
            ],
            digests=[EvidenceDigest(session_id="s", summary_text=f"Shared {AWS_KEY_ID}.")],
        )
    )
    chunks = {r["chunk_index"]: r for r in ch.inserts["loom.memory_chunks"]}
    assert set(chunks) == {0, 1}
    assert (chunks[0]["embedding_dim"], chunks[1]["embedding_dim"]) == (2, 0)
    assert chunks[1]["content"] == f"key {OPENAI_KEY}"  # stored as written
    [digest] = ch.inserts["loom.session_summaries"]
    assert digest["embedding_dim"] == 0 and digest["summary_text"] == f"Shared {AWS_KEY_ID}."
    assert (out.chunks_written, out.digests_written, out.skipped) == (2, 1, 1)
    assert out.embedding_dim == 2
    assert emb.seen == [_PLAIN]


@pytest.mark.asyncio
async def test_a_key_split_across_two_chunks_is_exported_as_held_and_imported_without_a_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write path cuts the text at 800 characters with a 100-character overlap, so the
    first chunk ends with the key's head, which does not read as a credential on its own: the
    export holds it by its missing vector, and the import reads the chunks joined at their
    overlap, so no piece of the key reaches the embedder."""
    import server.embedding as embmod
    import server.memory.store as memory_repo
    from server.memory.api.models_evidence_transfer import (
        MemoryExportEvidenceInput,
        MemoryImportEvidenceInput,
    )
    from server.memory.app import chunk_links, evidence_transfer
    from server.memory.app.chunk_projection import chunk_text_with_ranges
    from server.memory.domain.sensitivity import overlaps_credential, reads_high
    from server.observability import audit

    text = ("plain words here " * 60)[:784] + f" {OPENAI_KEY} " + "more plain words " * 60
    chunks, ranges, _ = chunk_text_with_ranges(text, max_chunks=64)
    contents = [c for c, _, _ in chunks]
    assert OPENAI_KEY[:8] in contents[0] and not reads_high(contents[0])
    held_on_write = overlaps_credential(text, ranges)
    assert held_on_write == [True, True, False]
    stored = [
        {"session_id": "s", "chunk_index": i, "content": c, "embedding_dim": 0 if h else 2}
        for i, (c, h) in enumerate(zip(contents, held_on_write, strict=True))
    ]

    class _Export:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return stored

    async def no_links(*_: Any, **__: Any) -> dict[tuple[str, int], list[str]]:
        return {}

    async def no_high_keys(*_: Any, **__: Any) -> set[str]:
        return set()

    audited: list[dict[str, Any]] = []

    async def emit_many(event: str, *, subjects: list[dict[str, Any]], **_: Any) -> None:
        audited.extend(subjects)

    monkeypatch.setattr(evidence_transfer, "get_client", lambda: _Export())
    monkeypatch.setattr(chunk_links, "chunk_memory_keys", no_links)
    monkeypatch.setattr(memory_repo, "high_keys", no_high_keys)
    monkeypatch.setattr(audit, "emit_many", emit_many)

    def export(include_high: bool) -> Any:
        return evidence_transfer.handle_memory_export_evidence(
            MemoryExportEvidenceInput(org="o", namespace="n", include_high=include_high)
        )

    held = await export(include_high=False)
    assert [c.chunk_index for c in held.chunks] == [2] and held.held_high == 2
    assert audited == []
    released = await export(include_high=True)
    assert [c.chunk_index for c in released.chunks] == [0, 1, 2]
    assert [a["chunk_index"] for a in audited] == [0, 1]

    async def not_refused(*_: Any, **__: Any) -> bool:
        return False

    ch, emb = _CH(), _Embedder()
    monkeypatch.setattr(evidence_transfer, "get_client", lambda: ch)
    monkeypatch.setattr(embmod, "get_embedder", lambda: emb)
    monkeypatch.setattr(evidence_transfer, "_refused", not_refused)
    monkeypatch.setattr(evidence_transfer, "_importer_user", lambda _input: None)
    await evidence_transfer.handle_memory_import_evidence(
        MemoryImportEvidenceInput(
            org="o", namespace="other", agent="a", chunks=list(reversed(released.chunks))
        )
    )
    dims = {r["chunk_index"]: r["embedding_dim"] for r in ch.inserts["loom.memory_chunks"]}
    assert dims == {0: 0, 1: 0, 2: 2}
    assert emb.seen == [contents[2]]
    pieces = {OPENAI_KEY[i : i + 8] for i in range(len(OPENAI_KEY) - 7)}
    assert not any(piece in seen for piece in pieces for seen in emb.seen)


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
        {"memory_key": "chunk:s:0", "content_full": f"[user] here is the key {OPENAI_KEY}"},
        {"memory_key": "chunk:s:1", "content_full": _PLAIN},
        {"memory_key": "chunk:s:2", "content_full": _PLAIN},
    ]
    assert [h.memory_key for h in _served(hits, rows, 5)] == ["chunk:s:1", "chunk:s:2"]
    # A held-back chunk does not take one of the `n` places.
    assert [h.memory_key for h in _served(hits, rows, 1)] == ["chunk:s:1"]


def test_a_chunk_key_with_a_credential_version_is_held_back() -> None:
    """Two stored versions of one chunk key, the plain one read first: the hit is held
    back, since the version that reads as a credential could be the one it serves."""
    from server.memory.app.retrieval.chunks import _served

    hits = [_hit("chunk:s:0", "[user] notes", 0.9), _hit("chunk:s:1", _PLAIN, 0.8)]
    rows = [
        {"memory_key": "chunk:s:0", "content_full": "[user] notes on the plan"},
        {"memory_key": "chunk:s:0", "content_full": f"[user] notes and the key {OPENAI_KEY}"},
        {"memory_key": "chunk:s:1", "content_full": _PLAIN},
    ]
    assert [h.memory_key for h in _served(hits, rows, 5)] == ["chunk:s:1"]


@pytest.mark.asyncio
async def test_the_summary_leg_serves_no_digest_that_reads_as_a_credential() -> None:
    from server.memory.app.retrieval.summaries import fetch_session_summaries

    class _Rows:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [
                {
                    # The key straddles the served excerpt's 1200-character cut.
                    "session_id": "s1",
                    "summary_text": "x" * 1195 + f" {AWS_KEY_ID}.",
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
            {"memory_key": "chunk:s:0", "content_excerpt": f"deploy key {OPENAI_KEY}"},
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
            return SimpleNamespace(text=f"The user pasted {OPENAI_KEY}.", tokens_in=1, tokens_out=1)

    emb = _Embedder()
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    monkeypatch.setattr(session_summary, "get_embedder", lambda: emb)
    draft = await session_summary._draft_session_summary(
        SimpleNamespace(session_id="s"), [{"role": "user", "content": "x"}], None
    )
    assert draft is not None
    assert draft.embedding == [] and emb.seen == []
    assert OPENAI_KEY in draft.summary_text  # stored as written


@pytest.mark.asyncio
async def test_literal_completion_counts_a_credential_turn_but_never_shows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import ask_literal_evidence as le

    async def fetch(**_: Any) -> list[dict[str, Any]]:
        return [
            {"session_id": "s", "observed_at": "2026-01-01 08:00:00", "content": "Stripe is down"},
            {
                "session_id": "s",
                "observed_at": "2026-01-01 09:00:00",
                "content": f"my Stripe key is {OPENAI_KEY}",
            },
        ]

    monkeypatch.setattr(le, "fetch_turns_with_literal", fetch)
    search = SimpleNamespace(org="o", namespace="n")
    hits = await le.literal_complete(
        search, "How many times did I mention 'Stripe'?", [], max_hits=10
    )
    assert [h.content_excerpt for h in hits] == ["[user @ 2026-01-01 08:00] Stripe is down"]
    assert le.LAST_LITERAL_STATEMENTS.get() == 2


class _LinkedTurnsCH:
    """The two reads of `fetch_raw_evidence`: the link rows, then the evidence rows."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls = 0

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.calls += 1
        if self.calls == 1:
            return [
                {
                    "memory_key": "m1",
                    "support_type": "extracted_from",
                    "char_start": 0,
                    "char_end": 10,
                    "evidence_id": f"ev-{i}",
                }
                for i in range(len(self.contents))
            ]
        return [
            {"evidence_id": f"ev-{i}", "role": "user", "observed_at": "", "content": c}
            for i, c in enumerate(self.contents)
        ]


@pytest.mark.asyncio
async def test_the_deep_evidence_pass_opens_no_turn_that_reads_as_a_credential() -> None:
    from server.memory.app.deep_evidence import fetch_raw_evidence

    turns = await fetch_raw_evidence(
        org="o",
        namespace="n",
        memory_keys=["m1"],
        max_chars_per_memory=1000,
        max_chars_total=5000,
        ch=_LinkedTurnsCH([f"deploy key {OPENAI_KEY}", _PLAIN]),
    )
    assert [t.content for t in turns] == [_PLAIN]


@pytest.mark.asyncio
async def test_a_summary_hit_opens_no_session_turn_that_reads_as_a_credential() -> None:
    from server.memory.app.deep_evidence import fetch_session_turns

    rows = [
        {"session_id": "s1", "role": "user", "observed_at": "", "content": c}
        for c in (f"the standup key is {AWS_KEY_ID}", _PLAIN)
    ]

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            return rows

    turns = await fetch_session_turns(
        org="o",
        namespace="n",
        session_ids=["s1"],
        question="When is the standup?",
        max_chars_per_session=1000,
        max_chars_total=5000,
        ch=_CH(),
    )
    assert [t.content for t in turns] == [_PLAIN]


@pytest.mark.asyncio
async def test_the_when_evidence_pass_recalls_no_turn_that_reads_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.recall as recall
    from server.memory.app import ask_when_evidence as awe

    async def fake_recall(_: Any) -> SimpleNamespace:
        return SimpleNamespace(
            turns=[
                SimpleNamespace(role="user", observed_at="2026-01-02", content=_PLAIN),
                SimpleNamespace(
                    role="user", observed_at="2026-01-03", content=f"key: {OPENAI_KEY}"
                ),
            ]
        )

    monkeypatch.setattr(recall, "handle_memory_recall", fake_recall)
    turns = await awe._recall_turns(
        {"org": "o", "namespace": "n", "agent": "*", "query": "standup"}
    )
    assert [t.content for t in turns] == [_PLAIN]
