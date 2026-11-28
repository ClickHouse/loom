"""The lexical gate admits every scored identifier and rejects partial tokens, and
the pool is cut on the vote the path penalties leave each row."""

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


@pytest.mark.asyncio
async def test_the_pool_is_cut_on_the_vote_each_row_has_in_the_ranking() -> None:
    rows = (
        _row("tests/test_merge.py", "merge tree select"),
        _row("docs/merge.txt", "merge tree select"),
        _row("src/merge.py", "merge tree"),
        _row("src/CMakeLists.txt", "merge tree"),
    )
    tokens, idfs = ["merge", "tree", "select"], [1.0, 1.0, 1.0]
    first_two = {"tests/test_merge.py", "docs/merge.txt"}

    async def leg(n: int, **penalties: float) -> list[dict]:
        return await _repo(*rows).lexical_search_chunks(
            "o", "n", "m", tokens, idfs, pool=n, **penalties
        )

    def pool(found: list[dict]) -> list[tuple[str, float, int]]:
        return [(r["path"], r["lex_score"], r["lex_rank"]) for r in found if r["in_pool"]]

    # the test and the doc match one more term, so they are the leg's first two rows
    assert {r["path"] for r in await leg(2)} == first_two
    # a source row at rank 3 votes 1/63, the test row at most 0.35/61: the pool goes
    # to the source rows, which keep their ranks in the whole leg
    cut = await leg(2, noise_penalty=0.35, prose_penalty=0.25)
    assert {p for p, _, _ in pool(cut)} == {"src/merge.py", "src/CMakeLists.txt"}
    assert [(score, rank) for _, score, rank in pool(cut)] == [(2.0, 3), (2.0, 4)]
    # the leg's own first two rows come back beside the pool, in rank order
    assert [r["lex_rank"] for r in cut] == [1, 2, 3, 4]
    assert {r["path"] for r in cut if not r["in_pool"]} == first_two
    # a test row that makes the cut keeps its rank and its score
    kept = pool(await leg(3, noise_penalty=0.35, prose_penalty=0.25))
    assert kept[0][:2] == ("tests/test_merge.py", 3.0) and kept[0][2] in (1, 2)
    assert {p for p, _, _ in kept[1:]} == {"src/merge.py", "src/CMakeLists.txt"}
    # a question that asks for tests cuts on the prose factor alone
    asked = pool(await leg(2, prose_penalty=0.25))
    assert asked[0][:2] == ("tests/test_merge.py", 3.0) and asked[1][0].startswith("src/")


@pytest.mark.asyncio
async def test_a_first_ranked_test_row_outlasts_source_rows_that_match_less() -> None:
    """The test row matches all three terms and 130 source rows match two. Its vote,
    0.35/61, is worth a source row at rank 115, so a pool of 120 keeps it at rank 1
    beside the 119 best source rows. A cut on score times factor (3 x 0.35 under 2)
    would have dropped it for any pool under 131."""
    source = (
        "INSERT INTO loom.code_chunks (org, namespace, embedding_model, path, chunk_id, "
        "content, lexemes, embedding) SELECT 'o', 'n', 'm', concat('src/m', toString(number), "
        "'.py'), generateUUIDv4(), '', 'merge tree', arrayResize([0.1], 3072, 0.1) "
        "FROM numbers(130)"
    )
    repo = _repo(_row("tests/test_merge.py", "merge tree select"), source)
    found = await repo.lexical_search_chunks(
        "o", "n", "m", ["merge", "tree", "select"], [1.0, 1.0, 1.0], pool=120, noise_penalty=0.35
    )
    assert [(r["lex_rank"], bool(r["in_pool"])) for r in found] == [
        (rank, True) for rank in range(1, 121)
    ]
    assert found[0]["path"] == "tests/test_merge.py"
