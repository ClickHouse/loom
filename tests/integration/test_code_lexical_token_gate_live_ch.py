"""Stored underscore identifiers remain visible through the lexical text-index gate."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from server.code.domain.extract.tokenize import code_tokens
from server.code.store.index import CodeRepo
from server.core.ch import get_client, set_query_tag
from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_lexical_gate_reads_existing_identifier_lexemes(ch_client: Any) -> None:
    org, ns = unique_org("lexical-gate"), "code"
    ch_client.insert(
        "loom.code_chunks",
        [
            [org, ns, "m", path, uuid.uuid4(), lexemes, [0.0] * 3072]
            for path, lexemes in [
                ("src/constructor.py", "__init__"),
                ("src/private.py", "_private"),
                ("src/underscores.py", "__"),
                ("src/quota.py", "quota_store quota store"),
                ("src/partials.py", "init private"),
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "path",
            "chunk_id",
            "lexemes",
            "embedding",
        ],
    )
    client = get_client()
    await client.connect()
    set_query_tag("test.code.lexical_token_gate")
    repo = CodeRepo(client)
    for query, expected in [
        ("__init__", "src/constructor.py"),
        ("_private", "src/private.py"),
        ("__", "src/underscores.py"),
        ("quota_store", "src/quota.py"),
    ]:
        tokens = code_tokens(query)
        rows = await repo.lexical_search_chunks(org, ns, "m", tokens, [1.0] * len(tokens))
        assert [r["path"] for r in rows] == [expected]
        assert rows[0]["lex_score"] == len(tokens)
