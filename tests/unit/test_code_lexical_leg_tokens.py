"""The code lexical leg binds the query's own tokens.

There is no query-side morphology (adding corpus-known inflections of prose
tokens): the corpus-stats read and the lexical scan bind exactly
`code_tokens(query)`, and nothing is disclosed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.domain.extract.tokenize import code_tokens
from server.settings import settings

# ---- through the service ---------------------------------------------------

_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


class _Repo:
    """Records the token lists the two lexical statements bind."""

    def __init__(self, df: dict[str, int]) -> None:
        self.df = df
        self.stats_tokens: list[str] = []
        self.scan_tokens: list[str] = []
        self.scan_idfs: list[float] = []

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        self.stats_tokens = list(tokens)
        return {t: self.df[t] for t in tokens if t in self.df}, 1000, 50.0

    async def lexical_search_chunks(
        self, org: str, ns: str, model: str, tokens: list[str], idfs: list[float], **k: Any
    ) -> list[dict[str, Any]]:
        self.scan_tokens = list(tokens)
        self.scan_idfs = list(idfs)
        return [{"chunk_id": "a", "lex_score": 4.0, "path": "src/a.cpp", **_UNIT}]

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return []

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


_Q = "where do we retry uploads in parseQuotaKey"
_DF = {
    "where": 500,
    "retry": 30,
    "uploads": 8,
    "parsequotakey": 2,
    "parse": 200,
    "quota": 40,
    "key": 900,
    "upload": 25,
    "uploading": 4,
    "retries": 12,
    "parses": 6,
}


def _run(repo: _Repo, **kw: Any) -> Any:
    with patch.object(settings, "code_fusion_signals", False):
        return asyncio.run(
            cs.search(repo, "o", "n", query=_Q, top_k=3, embedder=None, legs="lexical", **kw)
        )


def test_the_lexical_leg_binds_the_query_tokens_and_nothing_else() -> None:
    repo = _Repo(_DF)
    with patch.object(settings, "code_corpus_stats_ttl_s", 0):
        res = _run(repo)
    expected = code_tokens(_Q)
    # neither statement probes an inflection the query did not contain
    assert repo.stats_tokens == expected and repo.scan_tokens == expected
    assert "upload" not in repo.scan_tokens and len(repo.scan_idfs) == len(expected)
    assert "morphology_added" not in res.retrieval_work
    assert "morphology" not in res.score_explanation
