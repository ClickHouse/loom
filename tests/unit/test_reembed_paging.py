"""`loom reembed` pages each table in sort-key order, `batch` rows at a
time, and prints a resume token after every page; `--resume <token>`
skips the finished tables and continues after that row."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.ch import sequential_reads_active
from server.memory.app import reembed as rb
from tests.unit._fake_credentials import OPENAI_KEY
from tests.unit.clickhouse_local import CLICKHOUSE, EMBEDDINGS, VECTOR, run


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
                "agent": "a",
                "ts": i,
                "tombstoned_at": None,
            }
            for i in range(n)
        ]
        self.pages: list[dict[str, Any]] = []
        self.inserted: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "system.columns" in sql:
            return [{"name": c} for c in self.rows[0]]
        p = dict(params or {})
        if "keys" in p:  # the re-read before an insert, by the page's keys
            assert "tombstoned_at IS NULL" in sql and sequential_reads_active()
            return [
                dict(r) for r in self.rows if key(r) in p["keys"] and r["tombstoned_at"] is None
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
    table, key, _ = rb.decode_resume(token)
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
            if "keys" in (params or {}):  # by the key the row was read with, not the new model's
                assert [k[6] for k in params["keys"]] == ["m1"]
                return [dict(row)]
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
async def test_a_row_removed_while_its_page_is_embedded_is_not_written_back() -> None:
    """A redaction deletes a key's rows, an erasure tombstones them and a write gives a
    row other text: the page read before must not insert its old copy again. A row
    changed otherwise (handed to another agent at the same stamp) is written with its
    columns as stored now, so --delete-old leaves it a vector."""
    ch, lines, written = _CH(5), [], []

    class _Redacting(_Emb):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            deleted, rewritten, handed, tombstoned = ch.rows[1:]
            ch.rows.remove(deleted)
            rewritten["content_excerpt"] = "other text"
            handed["agent"] = "peer"
            tombstoned["tombstoned_at"] = "now"
            return await super().embed_many(texts)

    insert = ch.insert

    async def _insert(table: str, rows: list[dict], settings: Any = None) -> None:
        written.extend(rows)
        await insert(table, rows, settings)

    ch.insert = _insert  # type: ignore[method-assign]
    n = await rb.reembed_table(ch, _Redacting(), "conversation_embeddings", log=lines.append)
    assert n == 2 and ch.inserted == ["000", "003"]
    assert [r["agent"] for r in written] == ["a", "peer"]
    assert all(r["embedding_model"] == "m2" and r["embedding"] == [0.1, 0.2] for r in written)
    assert any("3 removed or rewritten since read, skipped" in line for line in lines)


@pytest.mark.asyncio
async def test_delete_old_keeps_the_old_rows_of_an_org_with_a_skipped_row(monkeypatch) -> None:
    """A row given other text while its page was embedded has no new-model copy yet: its
    org's old-model rows stay for a later run."""
    ch, deletes = _CH(2), []

    class _Rewriting(_Emb):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            ch.rows[1]["content_excerpt"] = "other text"
            return await super().embed_many(texts)

    async def execute(sql: str, params: Any = None, **_: Any) -> None:
        deletes.append(params or {})

    async def width(table: str, dim: int) -> None:
        return None

    ch.execute = execute  # type: ignore[attr-defined]
    monkeypatch.setattr("server.core.vectors.codes_state.refuse_width", width)
    real = rb.reembed_table

    async def only_turns(ch: Any, embedder: Any, table: str, **kw: Any) -> int:
        if table != "conversation_embeddings":
            return 0
        return await real(ch, embedder, table, **kw)

    monkeypatch.setattr(rb, "reembed_table", only_turns)
    await rb.reembed_all(ch, _Rewriting(), delete_old=True, log=lambda *_: None)
    assert ch.inserted == ["000"]
    assert deletes and all(p.get("off") == ["o"] for p in deletes)


@pytest.mark.asyncio
async def test_a_page_larger_than_one_re_read_is_read_again_in_parts(monkeypatch) -> None:
    ch, sql_with_keys = _CH(5), []
    query_all = ch.query_all

    async def counting(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "keys" in (params or {}):
            sql_with_keys.append(len(params["keys"]))
        return await query_all(sql, params)

    ch.query_all = counting  # type: ignore[method-assign]
    real_chunks = rb.chunks
    monkeypatch.setattr(rb, "chunks", lambda values: real_chunks(values, limit=70))
    n = await rb.reembed_table(ch, _Emb(), "conversation_embeddings", log=lambda *_: None)
    assert n == 5 and ch.inserted == ["000", "001", "002", "003", "004"]
    assert sql_with_keys == [2, 2, 1]


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
@pytest.mark.asyncio
async def test_the_re_read_finds_a_live_row_by_its_key_and_leaves_its_vectors_out() -> None:
    """On the production table definition: the Enum `scope` matches by name, a tombstoned
    or lightweight-deleted row is not found, and no vector column is read."""
    table = "memory_embeddings"
    head = "org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key"
    values = ", ".join(
        f"('o','n','a','-','-','private','m1','{key}','x',{VECTOR},{VECTOR},2,{tombstone})"
        for key, tombstone in (("live", "NULL"), ("tombstoned", "now()"), ("deleted", "NULL"))
    )
    setup = [
        *EMBEDDINGS,
        f"INSERT INTO loom.{table} ({head}, content_excerpt, embedding, memory_key_embedding, "
        f"embedding_dim, tombstoned_at) VALUES {values}",
        f"DELETE FROM loom.{table} WHERE memory_key = 'deleted'",
    ]
    cols = [
        r["name"]
        for r in run(
            setup,
            "SELECT name FROM system.columns WHERE database = 'loom' "
            f"AND table = '{table}' AND default_kind != 'MATERIALIZED' ORDER BY position",
        )
    ]
    where, params = rb.where_sql(table, cols, "m2", org="o", namespace=None)

    class _Capture:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
            self.sql, self.params = sql, params
            return []

    def row(k: str) -> dict[str, Any]:
        return dict(
            zip(head.split(", "), ["o", "n", "a", "-", "-", "private", "m1", k], strict=True)
        )

    capture = _Capture()
    read = {
        i: {**row(k), "content_excerpt": "x"}
        for i, k in enumerate(["live", "tombstoned", "deleted"])
    }
    await rb._still_stored(capture, table, cols, read, where, params)
    found = run(setup, capture.sql, capture.params)
    assert [r["memory_key"] for r in found] == ["live"]
    assert not {"embedding", "memory_key_embedding"} & set(found[0])


@pytest.mark.asyncio
async def test_a_key_only_row_rewritten_on_the_current_model_keeps_no_org() -> None:
    """It already has its vectors: --delete-old need not wait for it."""
    row = {
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "scope": "private",
        "embedding_model": "m2",
        "memory_key": "k",
        "content_excerpt": "x",
        "embedding": [0.0, 0.0],
        "embedding_dim": 2,
        "memory_key_embedding": [0.0, 0.0],
        "memory_key_embedding_dim": 2,
        "version": 1,
    }

    class _KeyCH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict]:
            if "system.columns" in sql:
                return [{"name": c} for c in row]
            if "keys" in (params or {}):
                return [{**row, "content_excerpt": "other text"}]
            return [] if "k0" in (params or {}) else [{**row, "_k": tuple(row.values())[:8]}]

        async def query_one(self, sql: str, params: Any = None) -> dict:
            return {"n": 1}

        async def insert(self, table: str, rows: list[dict], settings: Any = None) -> None:
            raise AssertionError("a rewritten row is not written")

    kept: set[str] = set()
    n = await rb.reembed_table(
        _KeyCH(), _Emb(), "memory_embeddings", key_only=True, left_orgs=kept, log=lambda *_: None
    )
    assert n == 0 and kept == set()


@pytest.mark.asyncio
async def test_a_resume_token_carries_the_orgs_delete_old_keeps(monkeypatch) -> None:
    """A run interrupted after skipping a rewritten row: the resumed run's --delete-old
    still keeps that org's old-model rows."""
    key = ["o", "ns", "m1", "t", "001"]
    assert rb.decode_resume(rb.encode_resume("conversation_embeddings", key))[2] == []
    token = rb.encode_resume("conversation_embeddings", key, ["o"])
    assert rb.decode_resume(token) == ("conversation_embeddings", key, ["o"])

    ch, deletes = _CH(3), []

    async def execute(sql: str, params: Any = None, **_: Any) -> None:
        deletes.append(params or {})

    async def width(table: str, dim: int) -> None:
        return None

    ch.execute = execute  # type: ignore[attr-defined]
    monkeypatch.setattr("server.core.vectors.codes_state.refuse_width", width)
    await rb.reembed_all(ch, _Emb(), resume=token, delete_old=True, log=lambda *_: None)
    assert ch.inserted == ["002"] and deletes and all(p.get("off") == ["o"] for p in deletes)
