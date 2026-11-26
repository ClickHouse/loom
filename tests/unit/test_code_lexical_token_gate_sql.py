"""The lexical gate admits every scored identifier and rejects partial tokens."""

from __future__ import annotations

import uuid

import pytest

from server.code.domain.extract.tokenize import code_tokens, lexemes_text
from server.code.store.index import CodeRepo
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _row(path: str, lexemes: str, *, org: str = "o", ns: str = "n", model: str = "m") -> str:
    cid = uuid.uuid5(uuid.NAMESPACE_URL, f"{org}/{ns}/{model}/{path}")
    return (
        "INSERT INTO loom.code_chunks (org, namespace, embedding_model, path, chunk_id, "
        "content, lexemes, embedding) VALUES "
        f"('{org}', '{ns}', '{model}', '{path}', '{cid}', '', '{lexemes}', "
        "arrayResize([0.1], 3072, 0.1))"
    )


def _repo(*rows: str) -> CodeRepo:
    return CodeRepo(  # type: ignore[arg-type]
        LocalCH("SET enable_quantized_codec = 1", ddl("code_chunks"), *rows)
    )


@pytest.mark.parametrize(
    "identifier", ["__init__", "_private", "_x", "__", "quota_store", "parseQuotaKey"]
)
@pytest.mark.asyncio
async def test_stored_identifier_survives_the_text_index_gate(identifier: str) -> None:
    tokens = code_tokens(identifier)
    repo = _repo(_row("src/target.py", lexemes_text(identifier)))
    rows = await repo.lexical_search_chunks("o", "n", "m", tokens, [1.0] * len(tokens))
    assert [r["path"] for r in rows] == ["src/target.py"]
    assert rows[0]["lex_score"] == len(tokens)


@pytest.mark.parametrize(
    "query,partial", [("__init__", "init"), ("_private", "private"), ("_x", "x")]
)
@pytest.mark.asyncio
async def test_gate_subtokens_without_an_exact_lexeme_do_not_become_hits(
    query: str, partial: str
) -> None:
    repo = _repo(_row("src/partial.py", partial))
    rows = await repo.lexical_search_chunks("o", "n", "m", code_tokens(query), [1.0])
    assert rows == []


@pytest.mark.asyncio
async def test_identifier_gate_keeps_identity_model_prefix_and_pool() -> None:
    repo = _repo(
        _row("src/one.py", "__init__"),
        _row("src/two.py", "__init__"),
        _row("other/hidden.py", "__init__"),
        _row("src/org.py", "__init__", org="other"),
        _row("src/ns.py", "__init__", ns="other"),
        _row("src/model.py", "__init__", model="other"),
    )
    rows = await repo.lexical_search_chunks(
        "o", "n", "m", ["__init__"], [1.0], path_prefix="src/", pool=1
    )
    assert len(rows) == 1 and rows[0]["path"] in {"src/one.py", "src/two.py"}
