"""The code lexical leg's statement: the idf-weighted count over the gated rows,
held byte for byte, and the corpus statistics its idfs come from."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store.index import CodeRepo

# The statement CodeRepo.lexical_search_chunks builds, with the path
# prefix off; the second blank line is the constrained-search scope slot, empty
# here.
_TODAY = (
    """
            SELECT chunk_id, path, scope, kind, start_line, end_line,
                   content, symbol_ids, context_description,
                   arraySum(
                       arrayMap(
                           (tok, idf) -> idf * countSubstrings(
                               concat(' ', lexemes, ' '), concat(' ', tok, ' ')
                           ),
                           {tokens:Array(String)}, {idfs:Array(Float32)}
                       )
                   ) AS lex_score
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND tombstoned_at IS NULL
              AND hasAnyTokens(lexemes, {tokens:Array(String)})
"""
    + " " * 14
    + "\n"
    + " " * 14
    + """
            ORDER BY lex_score DESC
            LIMIT {pool:UInt32}
            """
)


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.one_calls: list[tuple[str, dict[str, Any]]] = []
        self.one_row: dict[str, Any] | None = {"n": 100, "avgdl": 56.0}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        self.one_calls.append((sql, dict(params or {})))
        return self.one_row


def _statement(**kw: Any) -> tuple[str, dict[str, Any]]:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    asyncio.run(repo.lexical_search_chunks("o", "n", "m", ["merge", "tree"], [2.5, 0.7], **kw))
    assert len(ch.calls) == 1
    return ch.calls[0]


def test_lexical_statement_byte_for_byte() -> None:
    sql, params = _statement()
    assert sql == _TODAY
    assert params == {
        "org": "o",
        "ns": "n",
        "model": "m",
        "tokens": ["merge", "tree"],
        "idfs": [2.5, 0.7],
        "pool": 200,
    }


# ---- corpus stats ------------------------------------------------------------


def test_code_corpus_stats_returns_avgdl_from_the_same_read() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    df, n, avgdl = asyncio.run(repo.code_corpus_stats("o", "n", ["merge"]))
    assert (df, n, avgdl) == ({}, 100, 56.0)
    assert len(ch.one_calls) == 1
    sql, _ = ch.one_calls[0]
    assert "avgOrDefault(token_count)" in sql and "FROM loom.code_doc_stats FINAL" in sql


def test_code_corpus_stats_empty_namespace() -> None:
    ch = _CH()
    ch.one_row = {"n": 0, "avgdl": 0.0}
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    assert asyncio.run(repo.code_corpus_stats("o", "n", ["merge"])) == ({}, 0, 0.0)
    assert asyncio.run(repo.code_corpus_stats("o", "n", [])) == ({}, 0, 0.0)


class _SnapCH(_CH):
    """Serves `code_term_df` rows; every other read is recorded as a fallback."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self.rows = rows

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return self.rows if "FROM loom.code_term_df" in sql else []


def test_code_corpus_stats_reads_the_snapshot_in_one_statement() -> None:
    ch = _SnapCH(
        [
            {"term": "", "df": 351203, "avgdl": 61.5},
            {"term": "merge", "df": 4200, "avgdl": 0.0},
        ]
    )
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    got = asyncio.run(repo.code_corpus_stats("o", "n", ["merge", "the"]))
    assert got == ({"merge": 4200}, 351203, 61.5)
    assert len(ch.calls) == 1 and not ch.one_calls
    sql, params = ch.calls[0]
    assert "FROM loom.code_term_df" in sql and "code_term_freq" not in sql
    assert params["terms"] == ["", "merge", "the"]


def test_code_corpus_stats_falls_back_when_the_snapshot_lacks_the_namespace() -> None:
    # Term rows without the namespace's '' row are not trusted either.
    ch = _SnapCH([{"term": "merge", "df": 4200, "avgdl": 0.0}])
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    df, n, avgdl = asyncio.run(repo.code_corpus_stats("o", "n", ["merge"]))
    assert (df, n, avgdl) == ({}, 100, 56.0)
    assert [s for s, _ in ch.calls if "code_term_freq" in s]
    assert len(ch.one_calls) == 1 and "code_doc_stats FINAL" in ch.one_calls[0][0]


def test_code_term_df_refresh_replaces_the_snapshot_hourly() -> None:
    """The snapshot is rebuilt whole: an APPEND refresh would add a second copy of
    every row each hour, and the reader takes whichever row it meets first."""
    from tests.unit.schema_ddl import ddl

    view = ddl("code_term_df_refresh")
    assert "REFRESH EVERY 1 HOUR TO loom.code_term_df" in view
    assert "APPEND" not in view
    assert "FROM loom.code_term_freq" in view and "FROM loom.code_doc_stats" in view
    # the namespace row the reader keys its trust on
    assert "'' AS term" in view


def test_search_gates_on_every_query_token_and_times_the_leg() -> None:
    """The service hands the leg its tokens and idfs (every token gates) and
    records the leg's wall time."""
    from server.code.app import search as cs

    seen: dict[str, Any] = {}

    class Repo:
        async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
            return {t: 3 for t in tokens}, 100, 56.0

        async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            seen.update(args=a, kw=k)
            return [
                {
                    "chunk_id": "c1",
                    "lex_score": 4.0,
                    "path": "src/x.cpp",
                    "scope": "f",
                    "kind": "function",
                    "start_line": 1,
                    "end_line": 5,
                    "content": "x",
                    "symbol_ids": [],
                    "context_description": "",
                }
            ]

        async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
            return []

    async def run(**kw: Any) -> Any:
        return await cs.search(
            Repo(),
            "o",
            "n",
            query="how does the merge tree select parts",  # type: ignore[arg-type]
            embedder=None,
            **kw,
        )

    res = asyncio.run(run())
    tokens, idfs = seen["args"][3], seen["args"][4]
    assert tokens and len(idfs) == len(tokens)
    assert set(seen["kw"]) == {"path_prefix", "pool", "scope"}
    assert "lexical_leg_ms" in res.retrieval_work
