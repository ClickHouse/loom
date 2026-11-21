"""Search proof probes read exact live model keys from the actual chunk table."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.integration._resources import unique_org


@asynccontextmanager
async def chunks():
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.embedding import get_embedder
    from server.knowledge.app.source_access import Admitted
    from server.knowledge.app.source_search_proofs import SearchProofs

    embedder = get_embedder()
    assert embedder.model_name.startswith("fake-"), "This fixture uses only fake embeddings"
    vector = await embedder.embed("search proof fixture")
    org, namespace, uri = (
        unique_org("search-chunk-proof"),
        new_mirror_name(),
        "gdrive:file#" + str(uuid4()),
    )
    now = datetime.now(UTC).replace(microsecond=0)
    row = {
        "org": org,
        "namespace": namespace,
        "source_uri": uri,
        "embedding_model": embedder.model_name,
        "chunk_idx": 0,
        "doc_version": 9,
        "chunk_metadata": json.dumps({"revision": 0}),
        "embedding_dim": len(vector),
        "embedding": vector,
        "content": "Body excluded from a key proof",
        "context": "Private title",
        "doc_superseded_at": None,
    }
    item = Admitted(
        "team",
        {k: row[k] for k in ("namespace", "source_uri", "chunk_idx", "doc_version")}
        | {"revision": 0},
        {
            "namespace": namespace,
            "source_uri": uri,
            "tags": ["source-id:" + uri.rsplit("#", 1)[1]],
            "version": 10,
            "revision": 0,
            "ingested_at": now,
        },
    )
    plan = SimpleNamespace(
        org=org,
        failed=False,
        unread=False,
        current=lambda _item: True,
        check=AsyncMock(return_value=[item]),
    )
    proof = SearchProofs(plan, embedder.model_name, (item,))
    ch = get_client()
    set_query_tag("test.mirror.search_chunk_keys")
    try:
        await ch.connect()
        await ch.insert(
            "loom.chunks",
            [
                row,
                {**row, "chunk_idx": 1, "embedding_dim": 0},
                {**row, "chunk_idx": 2, "doc_superseded_at": now},
            ],
        )
        yield ch, row, proof
    finally:
        await ch.disconnect()


async def test_exact_live_key_scope_and_retirement_invalidate_a_real_search_proof():
    from server.knowledge.app.source_corpus import LostCorpus
    from server.knowledge.store import source_candidates as store

    async with chunks() as (ch, row, proof):
        org, ns, uri, model = (
            row["org"],
            row["namespace"],
            row["source_uri"],
            row["embedding_model"],
        )
        key = ns, uri, 0, 9, 0
        assert await store.chunk_keys(
            org, model, [key, (ns, uri, 1, 9, 0), (ns, uri, 2, 9, 0)]
        ) == [key]
        assert await store.chunk_keys(org + "-foreign", model, [key]) == []
        assert await store.chunk_keys(org, model + "-wrong", [key]) == []
        assert await store.chunk_keys(org, model, [(ns, uri, 0, 10, 0), (ns, uri, 0, 9, 1)]) == []
        await proof.check()
        await ch.insert(
            "loom.chunk_supersessions",
            [
                {
                    k: row[k]
                    for k in ("org", "namespace", "source_uri", "embedding_model", "doc_version")
                }
                | {"superseded_at": datetime.now(UTC).replace(microsecond=0)}
            ],
        )
        assert await store.chunk_keys(org, model, [key]) == []
        with pytest.raises(LostCorpus):
            await proof.check()
        assert proof.invalid


async def test_physical_duplicate_keys_refuse_an_ambiguous_search_proof():
    from server.knowledge.app.source_corpus import LostCorpus
    from server.knowledge.store import source_candidates as store

    async with chunks() as (ch, row, proof):
        key = row["namespace"], row["source_uri"], 0, 9, 0
        await ch.insert("loom.chunks", [row])
        assert await store.chunk_keys(row["org"], row["embedding_model"], [key]) == [key, key]
        with pytest.raises(LostCorpus):
            await proof.check()
        assert proof.invalid
