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
    # both match; the pool of one takes the first in path order
    assert [r["path"] for r in rows] == ["src/one.py"]


def _chunk(path: str, line: int, tag: str = "") -> str:
    """One chunk of `path` that starts at `line` and holds the lexeme `merge`."""
    cid = uuid.uuid5(uuid.NAMESPACE_URL, f"{path}:{line}:{tag}")
    return (
        "INSERT INTO loom.code_chunks (org, namespace, embedding_model, path, chunk_id, "
        "start_line, content, lexemes, embedding) VALUES "
        f"('o', 'n', 'm', '{path}', '{cid}', {line}, '', 'merge', arrayResize([0.1], 3072, 0.1))"
    )


@pytest.mark.asyncio
async def test_rows_of_one_score_come_in_file_order_whatever_part_holds_them() -> None:
    """Six chunks hold the one term and tie. Each insert is a part, and the table is
    written in two orders: the rows come back by path and then by start line both
    times, and the two chunks that start on one line in the same order."""
    chunks = [
        _chunk("src/b.py", 40),
        _chunk("src/a.py", 90),
        _chunk("src/b.py", 1),
        _chunk("src/a.py", 7, "first"),
        _chunk("src/a.py", 7, "second"),
        _chunk("lib/z.py", 300),
    ]

    async def leg(rows: list[str]) -> list[tuple[str, int, str]]:
        found = await _repo(*rows).lexical_search_chunks("o", "n", "m", ["merge"], [1.0])
        return [(r["path"], r["start_line"], r["chunk_id"]) for r in found]

    written, reversed_ = await leg(chunks), await leg(chunks[::-1])
    assert written == reversed_
    assert [(path, line) for path, line, _ in written] == [
        ("lib/z.py", 300),
        ("src/a.py", 7),
        ("src/a.py", 7),
        ("src/a.py", 90),
        ("src/b.py", 1),
        ("src/b.py", 40),
    ]
