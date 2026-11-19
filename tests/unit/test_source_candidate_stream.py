"""Ranked discovery streams carry metadata and close before text hydration."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from server.knowledge.store import source_candidates as store


@pytest.mark.parametrize("leg", ["semantic", "lexical", "keys"])
@pytest.mark.parametrize("authority", ["clickhouse", "postgres"])
def test_metadata_stream_has_no_page_cap_or_hidden_corpus_statistics(leg, authority):
    sql, params = store.statement(
        ["org = {org:String}", "namespace IN {nss:Array(String)}"],
        {
            "org": "acme",
            "nss": ["acl-" + "1" * 24],
            "q": [1.0],
            "toks": ["token"],
            "keys": ["object"],
            "idfs": [1000.0],
            "avgdl": 100000.0,
        },
        leg=leg,
        access_keys=["anyone"] if authority == "clickhouse" else None,
    )
    select = sql.split("SELECT", 1)[1].split("FROM loom.chunks", 1)[0]
    assert "source_uri" in select and "AS revision" in select
    assert "content," not in select and "context," not in select and "tags," not in select
    assert "LIMIT" not in sql and "OFFSET" not in sql
    assert "vector_search" not in sql and "chunk_term_freq" not in sql
    assert ("hasAny(access_keys" in sql) == (authority == "clickhouse")
    assert "FORMAT JSONEachRow" in sql
    if leg == "lexical":
        assert params["idfs"] == [1.0] and params["avgdl"] == 256.0
        assert "query_plan_direct_read_from_text_index = 0" in sql
    elif leg == "keys":
        assert "hasAny(keys" in sql and "arrayIntersect(keys" in sql


async def test_stream_decodes_split_unicode_and_closes_on_early_stop():
    class Client:
        closed = False

        @asynccontextmanager
        async def raw_stream(self, sql, params):
            assert "FORMAT JSONEachRow" in sql and params["org"] == "acme"

            async def body():
                raw = json.dumps({"source_uri": "drive:K"}, ensure_ascii=False).encode() + b"\n"
                for byte in raw:
                    yield bytes([byte])
                yield b'{"source_uri":"unused"}\n'

            try:
                yield body()
            finally:
                self.closed = True

    client = Client()
    async with store.candidates("SELECT 1 FORMAT JSONEachRow", {"org": "acme"}, ch=client) as rows:
        async for row in rows:
            assert row == {"source_uri": "drive:K"}
            break
        assert not client.closed
    assert client.closed


@pytest.mark.parametrize("raw", [b'{"x":1}\n{bad}\n', b"[]\n", b'{"x":1}\n{bad}'])
async def test_malformed_stream_never_becomes_a_partial_success(raw):
    class Client:
        closed = False

        @asynccontextmanager
        async def raw_stream(self, *_args):
            async def body():
                yield raw

            try:
                yield body()
            finally:
                self.closed = True

    client = Client()
    with pytest.raises(ValueError):
        async with store.candidates("SELECT 1", {}, ch=client) as rows:
            _ = [row async for row in rows]
    assert client.closed
