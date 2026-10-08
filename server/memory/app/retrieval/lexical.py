"""Lexical scoring for the fact leg: the token-overlap and BM25 expressions,
compound-token matching, and the corpus-stats read the BM25 arm binds.
"""

from __future__ import annotations

from typing import Any

from server.core import bm25 as _bm25
from server.memory.app.search_bm25 import (
    _bm25_sigmoid_params,
    compound_query_tokens,
    fetch_bm25_corpus_stats_probing,
)
from server.memory.domain.search_types import SearchQuery, _CHClient


def compound_match(excerpt: str, compounds: tuple[str, ...] | list[str]) -> bool:
    """Whether `excerpt` contains any of the query's known
    compounds, case-insensitively (the compound is lowercase; the row writes
    it CamelCase). Pure."""
    if not compounds or not excerpt:
        return False
    low = excerpt.lower()
    return any(c and c in low for c in compounds)


def bm25_lexical_expr(q: SearchQuery, params: dict[str, Any], tokens: list[str]) -> str:
    """The IDF-weighted Okapi BM25 lexical score over `e.content_excerpt`,
    logistic-squashed into [0, 1] — the `bm25_hybrid` arm's expression. Binds
    `query_tokens`, `bm25_idfs`, `bm25_avgdl`; the caller has checked that
    `q.bm25_idfs` aligns with `tokens` and `q.bm25_avgdl > 0`."""
    idfs = list(q.bm25_idfs)
    params["query_tokens"] = tokens or [""]
    params["bm25_idfs"] = idfs
    params["bm25_avgdl"] = float(q.bm25_avgdl)
    bm25_expr = _bm25.bm25_sum_sql(
        _bm25.tf_array_sql(
            "countSubstringsCaseInsensitive(e.content_excerpt, tok)", "{query_tokens:Array(String)}"
        ),
        "{bm25_idfs:Array(Float32)}",
        dl="length(e.content_excerpt)",
        avgdl="{bm25_avgdl:Float32}",
    )
    # Sigmoid scale = max achievable BM25 (every term saturates tf).
    max_score = sum(idfs) * (_bm25.K1 + 1) or 1.0
    _mid, _steep = _bm25_sigmoid_params(len(tokens), max_score)
    return f"1.0 / (1.0 + exp(-{_steep:.6f} * (({bm25_expr}) - {_mid:.6f})))"


def _lexical_score_expr(q: SearchQuery, params: dict[str, Any]) -> str:
    """The rank statement's lexical arm for the modes that carry one (lexical,
    hybrid, bm25_hybrid, graph, rrf): the greatest of the content arm, the
    key arm, the key exact match and the key loose match; "0.0" otherwise.
    Binds every parameter the expression names on `params`."""
    # Lexical match on content_excerpt AND memory_key — "what is X" queries
    # often name a key directly, so matching the key surfaces the exact-key
    # row alongside content matches (underscores <-> spaces normalized).
    # "rrf" is gated in because its Python fusion ranks one arm by this SQL
    # rank_score; without it that arm degenerates to semantic-only doubled.
    lexical_score_expr = "0.0"
    if q.search_mode in ("lexical", "hybrid", "bm25_hybrid", "graph", "rrf") and q.query_text:
        # Lemmatize so morphological variants match (deploys → deploy*).
        from server.memory.domain.lemmatize import lemma_tokens as _lemma_tokens
        from server.memory.domain.lemmatize import lemmagen_lemma_tokens as _lemmagen
        from server.memory.domain.lemmatize import lemmatize as _lemmatize
        from server.memory.domain.lemmatize import snowball_stem_tokens as _snowball

        lemmatized_query = _lemmatize(q.query_text) or q.query_text
        params["q_text"] = lemmatized_query
        params["q_text_loose"] = lemmatized_query.replace("_", " ")
        params["q_text_raw"] = q.query_text
        score_tokens = _lemma_tokens(q.query_text) or [lemmatized_query]
        # The compounds the corpus knows score like tokens.
        score_tokens = [*score_tokens, *[c for c in q.lexical_compounds if c not in score_tokens]]
        params["score_tokens"] = score_tokens
        # Snowball stems for content_excerpt_stem; Python snowballstemmer
        # matches CH's stem(t, 'en') byte-for-byte.
        params["score_stem_tokens"] = _snowball(score_tokens)
        # Lemmagen lemmas for content_excerpt_lemma; matches CH's
        # lemmatize('en', ...) and catches irregulars Snowball misses
        # (went/go, mice/mouse).
        params["score_lemma_tokens"] = _lemmagen(score_tokens)
        n_tokens = max(len(score_tokens), 1)
        # Max over four arms: content token-overlap fraction, key token-overlap
        # fraction (catches queries carrying the key's head noun but not its
        # value), key exact-full-query match, key loose (underscore<->space).
        # Token-overlap survives multi-word queries the full string never
        # matches contiguously.
        content_arm = (
            f"arraySum(arrayMap("
            "    t -> if(positionCaseInsensitive(e.content_excerpt, t) > 0, 1.0, 0.0), "
            "    {score_tokens:Array(String)}"
            f"  )) / {n_tokens}"
        )
        key_arm = (
            f"arraySum(arrayMap("
            "    t -> if(positionCaseInsensitive(e.memory_key, t) > 0, 1.0, 0.0), "
            "    {score_tokens:Array(String)}"
            f"  )) / {n_tokens}"
        )
        lexical_score_expr = (
            "greatest("
            f"  {content_arm},"
            f"  {key_arm},"
            "  if(positionCaseInsensitive(e.memory_key, {q_text_raw:String}) > 0, 1.0, 0.0),"
            "  if(positionCaseInsensitive(replaceAll(e.memory_key, '_', ' '), "
            "                              {q_text_loose:String}) > 0, 0.9, 0.0)"
            ")"
        )
    return lexical_score_expr


def stems_of_raw_tokens(lemmas: list[str], raw: list[str]) -> frozenset[str]:
    """The lemmas that are a raw query token's stem and not a raw token
    themselves. The term-frequency roll-up indexes raw words, so such a
    lemma is often unseen there, and an unseen token takes the maximum IDF
    and outweighs the word it came from: the precursor drops it when its df
    is 0 (the raw word still carries the term)."""
    from server.memory.domain.lemmatize import lemma_tokens as _lt

    raw_set = set(raw)
    stems = {lem for r in raw for lem in _bm25.floor_tokens(_lt(r))}
    return frozenset(lem for lem in lemmas if lem in stems and lem not in raw_set)


async def _bm25_precursor(ch: _CHClient, q: SearchQuery) -> SearchQuery:
    """Fetch per-token IDF + corpus avgdl from the MV-backed roll-ups for the
    modes that rank by BM25, and the adjacent-word compounds
    the roll-up knows. Cold roll-ups leave the query as is
    and the SQL path falls back to token overlap on its own."""
    from dataclasses import replace as _replace

    # BM25 precursor: fetch per-token IDF + corpus avgdl from MV-backed stats.
    # Cold MVs make the bm25_hybrid SQL path fall back to token-overlap on its
    # own, so no explicit fallback branching is needed here.
    if q.search_mode == "bm25_hybrid" and not q.bm25_idfs:
        from server.memory.domain.lemmatize import lemma_tokens as _lt

        # The term-freq MV indexes raw tokens; the query side uses lemmas.
        # Pass both forms so the IDF lookup hits whichever the MV indexed.
        # Every form keeps the MV's own length floor: a shorter token has no
        # df there, would take the maximum IDF, and its substring tf matches
        # almost every row.
        _lemmas = _bm25.floor_tokens(_lt(q.query_text) or [])
        # Raw tokenization mirroring the MV's splitByNonAlpha, which keeps
        # digits: a run id, a commit hash or a version is a term there, and
        # the rarest one a templated report carries.
        _raw = _bm25.query_tokens(q.query_text, min_len=_bm25.MIN_TERM_LEN)
        # De-dupe preserving order — the BM25 path indexes IDFs by position.
        _seen: set[str] = set()
        _tokens: list[str] = []
        for t in (*_lemmas, *_raw):
            if t and t not in _seen:
                _seen.add(t)
                _tokens.append(t)
        if _tokens:
            # Adjacent words the corpus writes as one term
            # (CamelCase alertnames, snake_case keys) join the tokens when
            # the roll-up knows them; an unknown compound is dropped.
            _final, idfs, avgdl = await fetch_bm25_corpus_stats_probing(
                ch,
                org=q.org,
                namespace=q.namespace,
                tokens=_tokens,
                probes=compound_query_tokens(_raw),
                drop_if_unseen=stems_of_raw_tokens(_lemmas, _raw),
            )
            _compounds = tuple(t for t in _final if t not in _seen)
            # SearchQuery is frozen — mutate via dataclasses.replace.
            q = _replace(
                q,
                bm25_tokens=_final,
                bm25_idfs=idfs,
                bm25_avgdl=avgdl,
                lexical_compounds=_compounds,
            )
    return q
