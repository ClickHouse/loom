"""Retirement removes unknown legacy content without treating it as a publishable copy."""

import pytest

from server.core.hashing import sha256_value
from server.memory.store import embeddings
from server.memory.store.rows import build_embedding_row, build_memory_version_row
from tests.unit.test_memory_verify import _row


@pytest.mark.parametrize("status", ["superseded", "rejected", "duplicate"])
@pytest.mark.parametrize("vector_version", [100, 250])
@pytest.mark.parametrize("hash_kind", ["missing", "different", "bytes"])
async def test_a_retirement_tombstones_unhashed_vectors_below_its_version(
    status, vector_version, hash_kind
):
    source = _row(value="current fact", status=status, version=200)
    canonical = build_memory_version_row(source)
    vector = build_embedding_row(
        **{
            k: canonical[k]
            for k in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
        },
        content_excerpt="legacy text",
        content_sha256=(
            ""
            if hash_kind == "missing"
            else sha256_value("legacy text").encode()
            if hash_kind == "bytes"
            else sha256_value("legacy text")
        ),
        embedding_model="legacy",
        embedding_dim=1,
        embedding=[0.5],
        version=vector_version,
    )

    class CH:
        def __init__(self):
            self.inserted = []

        async def query_all(self, sql, params=None):
            if "FROM loom.memory_embeddings" in sql:
                return [vector]
            if params and "keys" in params:
                return [canonical]
            return []

        async def insert(self, table, rows, **kwargs):
            assert table == "loom.memory_embeddings"
            self.inserted.extend(rows)

    ch = CH()
    count = await embeddings.mirror_status_to_embeddings([canonical], ch=ch, strict=True)
    assert count == len(ch.inserted) == int(vector_version < 200)
    if ch.inserted:
        (retired,) = ch.inserted
        assert retired["tombstoned_at"] is not None and retired["version"] == 200
        assert retired["content_excerpt"] == "" and retired["embedding"] == []
