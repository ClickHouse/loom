"""code.search keeps the corpus statistics its BM25 leg needs: df / N /
avgdl are read once per (namespace, term) inside the TTL, a
search reads only the tokens it has not seen, and a search whose tokens are
all known opens the lexical leg with no statistics read."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app.index import corpus_stats
from server.settings import settings

KNOWN = {"retry": 3, "upload": 7, "settings": 120}


class _Repo:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.n = 100
        self.avgdl = 56.0

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        self.calls.append(list(tokens))
        return {t: KNOWN[t] for t in tokens if t in KNOWN}, self.n, self.avgdl


def _stats(
    repo: _Repo, tokens: list[str], *, now: float, work: dict[str, int] | None = None
) -> Any:
    return asyncio.run(corpus_stats.stats(repo, "o", "n", tokens, work, now=now))


def test_second_search_reads_only_the_tokens_it_has_not_seen() -> None:
    repo = _Repo()
    with patch.object(settings, "code_corpus_stats_ttl_s", 300):
        w1: dict[str, int] = {}
        df, n, avgdl = _stats(repo, ["retry", "upload"], now=0.0, work=w1)
        assert (df, n, avgdl) == ({"retry": 3, "upload": 7}, 100, 56.0)
        assert repo.calls == [["retry", "upload"]] and w1["corpus_stats_terms_read"] == 2
        # every token known: no read at all, the same numbers
        w2: dict[str, int] = {}
        assert _stats(repo, ["upload", "retry"], now=10.0, work=w2) == (
            {"upload": 7, "retry": 3},
            100,
            56.0,
        )
        assert len(repo.calls) == 1 and w2 == {"corpus_stats_cached": 1}
        # one new token: only it is read; the rest come from the cache
        w3: dict[str, int] = {}
        df3, _n, _a = _stats(repo, ["retry", "settings"], now=20.0, work=w3)
        assert df3 == {"retry": 3, "settings": 120}
        assert repo.calls[-1] == ["settings"] and w3["corpus_stats_terms_read"] == 1
        assert corpus_stats.hits == 1 and corpus_stats.misses == 2


def test_an_unknown_token_is_remembered_as_unknown_and_stays_out_of_df() -> None:
    repo = _Repo()
    with patch.object(settings, "code_corpus_stats_ttl_s", 300):
        df, _n, _a = _stats(repo, ["retry", "zorblax"], now=0.0)
        assert df == {"retry": 3}  # the repo's shape: absent, not 0
        df2, _n, _a = _stats(repo, ["zorblax"], now=1.0)
        assert df2 == {} and len(repo.calls) == 1  # not asked again


def test_the_ttl_expires_the_whole_block() -> None:
    repo = _Repo()
    with patch.object(settings, "code_corpus_stats_ttl_s", 300):
        _stats(repo, ["retry"], now=0.0)
        repo.n = 250  # the index grew
        assert _stats(repo, ["retry"], now=299.0)[1] == 100  # still inside the TTL
        assert _stats(repo, ["retry"], now=301.0)[1] == 250  # re-read after it
        assert repo.calls == [["retry"], ["retry"]]


def test_ttl_zero_bypasses_the_cache() -> None:
    repo = _Repo()
    with patch.object(settings, "code_corpus_stats_ttl_s", 0):
        _stats(repo, ["retry"], now=0.0)
        _stats(repo, ["retry"], now=1.0)
        assert len(repo.calls) == 2 and corpus_stats.size() == (0, 0)


def test_the_term_cap_drops_the_oldest_term() -> None:
    repo = _Repo()
    with (
        patch.object(settings, "code_corpus_stats_ttl_s", 300),
        patch.object(settings, "code_corpus_stats_max_terms", 2),
    ):
        _stats(repo, ["retry"], now=0.0)
        _stats(repo, ["upload"], now=1.0)
        _stats(repo, ["settings"], now=2.0)
        assert corpus_stats.size() == (1, 2)
        _stats(repo, ["retry"], now=3.0)  # evicted: read again
        assert repo.calls[-1] == ["retry"]


def test_an_empty_namespace_is_not_remembered() -> None:
    repo = _Repo()
    repo.n = 0
    with patch.object(settings, "code_corpus_stats_ttl_s", 300):
        _stats(repo, ["retry"], now=0.0)
        _stats(repo, ["retry"], now=1.0)
        assert len(repo.calls) == 2 and corpus_stats.size() == (0, 0)


def test_a_repeated_search_opens_its_lexical_leg_without_the_stats_read() -> None:
    """Through `search()` itself, with the legs test's fake repo: the second
    search's span has no `stats` read and the fused result is the same."""
    from tests.unit.test_code_search_legs import _Embedder, _search
    from tests.unit.test_code_search_legs import _Repo as _LegsRepo

    with patch.object(settings, "code_corpus_stats_ttl_s", 300):
        first = _search(_LegsRepo(), _Embedder())
        repo2 = _LegsRepo()
        second = _search(repo2, _Embedder())
    assert [n for n, ev, _t in repo2.span if n == "stats"] == []
    assert [u.unit_id for u in second.units] == [u.unit_id for u in first.units]
    assert second.retrieval_work["corpus_stats_cached"] == 1
