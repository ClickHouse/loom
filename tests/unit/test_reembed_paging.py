"""`loom reembed` pages each table in sort-key order, `batch` rows at a
time, and prints a resume token after every page; `--resume <token>`
skips the finished tables and continues after that row."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import reembed as rb
from tests.unit._fake_credentials import OPENAI_KEY


class _Emb:
    model_name, dim = "m2", 2

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]


def key(r: dict[str, Any]) -> tuple[str, ...]:
    return (r["org"], r["namespace"], r["embedding_model"], r["thread_id"], r["msg_id"])


class _CH:
    """conversation_embeddings rows keyed (org, namespace, model, thread_id, msg_id);
    answers the keyset page read the way ClickHouse would."""

    def __init__(self, n: int) -> None:
        self.rows = [
            {
                "org": "o",
                "namespace": "ns",
                "embedding_model": "m1",
                "thread_id": "t",
                "msg_id": f"{i:03d}",
                "content_excerpt": f"c{i}",
                "embedding": [0.0],
                "embedding_dim": 1,
            }
            for i in range(n)
        ]
        self.pages: list[dict[str, Any]] = []
        self.inserted: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "system.columns" in sql:
            return [{"name": c} for c in self.rows[0]]
        p = dict(params or {})
        if "keys" in p:  # the re-read before an insert
            keys = [tuple(k) for k in p["keys"]]
            return [
                {"_k": list(key(r)), "_t": r["content_excerpt"]}
                for r in self.rows
                if key(r) in keys
            ]
        self.pages.append(p)
        after = tuple(p[f"k{i}"] for i in range(5)) if "k0" in p else None
        assert ("ORDER BY org, namespace, embedding_model, thread_id, msg_id" in sql) and (
            (after is None) == ("> ({k0:String}" not in sql)
        )
        left = sorted((r for r in self.rows if after is None or key(r) > after), key=key)
        return [{**r, "_k": key(r)} for r in left[: p["page"]]]

    async def query_one(self, sql: str, params: Any = None) -> dict:
        return {"n": len(self.rows)}

    async def insert(self, table: str, rows: list[dict], settings: Any = None) -> None:
        assert all("_k" not in r for r in rows)
        self.inserted.extend(r["msg_id"] for r in rows)


@pytest.mark.asyncio
async def test_pages_are_bounded_by_the_batch_and_a_token_resumes_after_its_row() -> None:
    ch, lines = _CH(5), []
    n = await rb.reembed_table(ch, _Emb(), "conversation_embeddings", batch=2, log=lines.append)
    assert n == 5 and ch.inserted == ["000", "001", "002", "003", "004"]
    assert [p["page"] for p in ch.pages] == [2, 2, 2]  # never more than a batch in memory
    token = next(line for line in lines if "3/5" in line or "4/5" in line).rsplit("resume: ", 1)[1]
    table, key = rb.decode_resume(token)
    assert table == "conversation_embeddings" and key[-1] == "003"

    ch2 = _CH(5)
    written = await rb.reembed_all(ch2, _Emb(), batch=2, resume=token, log=lambda *_: None)
    assert ch2.inserted == ["004"] and written == {"conversation_embeddings": 1}


def test_a_bad_token_is_refused() -> None:
    with pytest.raises(ValueError):
        rb.decode_resume("not-a-token")
    with pytest.raises(ValueError):
        rb.decode_resume(rb.encode_resume("memory_embeddings", ["only-one"]))


@pytest.mark.asyncio
async def test_a_row_whose_text_reads_as_a_credential_is_never_re_embedded() -> None:
    """It stays on the old model, which search does not read, and paging still moves past it."""
    ch, lines = _CH(3), []
    ch.rows[1]["content_excerpt"] = "my key is " + OPENAI_KEY
    sent: list[str] = []

    class _Recording(_Emb):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            sent.extend(texts)
            return await super().embed_many(texts)

    n = await rb.reembed_table(
        ch, _Recording(), "conversation_embeddings", batch=2, log=lines.append
    )
    assert n == 2 and ch.inserted == ["000", "002"]
    assert sent == ["c0", "c2"]
    assert any("reads as high" in line for line in lines)


@pytest.mark.asyncio
async def test_a_memory_row_is_re_embedded_whatever_its_text() -> None:
    """A memory row's vector follows its sensitivity label: a caller's `low` label on a
    value shaped like a credential wins, and its row has a vector to rebuild."""
    excerpt = "the test fixture key is " + OPENAI_KEY
    row = {
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "scope": "private",
        "embedding_model": "m1",
        "memory_key": "fixture_key",
        "content_excerpt": excerpt,
        "tags": ["sensitivity:low"],
        "embedding": [0.0],
        "embedding_dim": 1,
        "version": 1,
    }
    inserted: list[dict[str, Any]] = []
    sent: list[str] = []

    class _MemCH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            if "system.columns" in sql:
                return [{"name": c} for c in row]
            if "keys" in (params or {}):
                return [{"_k": [str(v) for v in tuple(row.values())[:8]], "_t": excerpt}]
            return [] if "k0" in (params or {}) else [{**row, "_k": tuple(row.values())[:8]}]

        async def query_one(self, sql: str, params: Any = None) -> dict:
            return {"n": 1}

        async def insert(self, table: str, rows: list[dict], settings: Any = None) -> None:
            inserted.extend(rows)

    class _Recording(_Emb):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            sent.extend(texts)
            return await super().embed_many(texts)

    n = await rb.reembed_table(_MemCH(), _Recording(), "memory_embeddings", log=lambda *_: None)
    assert n == 1 and sent[0] == excerpt
    assert inserted[0]["embedding_model"] == "m2"


@pytest.mark.asyncio
async def test_a_row_deleted_or_changed_while_its_page_is_embedded_is_not_written_back() -> None:
    """A redaction deletes a key's rows and an erasure deletes or tombstones them: the
    page read before it must not insert the old text again."""
    ch = _CH(3)

    class _Redacting(_Emb):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            del ch.rows[1]
            ch.rows[1]["content_excerpt"] = "rewritten"
            return await super().embed_many(texts)

    n = await rb.reembed_table(ch, _Redacting(), "conversation_embeddings", log=lambda *_: None)
    assert n == 1 and ch.inserted == ["000"]
