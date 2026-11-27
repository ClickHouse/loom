"""A long question is asked with its opening, its identifiers and its first page
: server/code/domain/index/query_trim.py and its seam in
code_search.search — byte-identical inputs under the cap, the opening and the
identifiers at the gate and the cleaned embedding text over it, and the
disclosure counters."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.index import query_trim as qt
from server.settings import settings

# ---- pure -------------------------------------------------------------------

_ISSUE = (
    "Company or project name xAI Describe what's wrong Jaccard Index calculated "
    "incorrectly when two same arrays are passed as arguments. "
    "https://fiddle.clickhouse.com/98b4bdfa-7c05008562fe How to reproduce "
    "SELECT arrayJaccardIndex([1,2],[1,2]) Expected behavior 1.0 Additional context "
    "_No response_ the function lives in ArrayJaccardIndex::executeImpl and "
    "parseQuotaKey is unrelated"
)


def test_strip_boilerplate_removes_the_flattened_headings_and_counts_them() -> None:
    cleaned, n = qt.strip_boilerplate(_ISSUE)
    assert n == 6
    for h in (
        "Company or project name",
        "Describe what's wrong",
        "How to reproduce",
        "Expected behavior",
        "Additional context",
        "_No response_",
    ):
        assert h.lower() not in cleaned.lower()
    assert "Jaccard Index calculated incorrectly" in cleaned
    assert "  " not in cleaned


def test_strip_boilerplate_leaves_a_plain_question_alone() -> None:
    q = "where SETTINGS secret values are masked before the query text is written"
    assert qt.strip_boilerplate(q) == (q, 0)
    # a heading inside a word is not a heading
    assert qt.strip_boilerplate("xhow to reproducex") == ("xhow to reproducex", 0)


def test_identifier_forms_keep_camel_snake_and_qualified_names() -> None:
    keep = qt.identifier_forms("parseQuotaKey quota_key_parser ArrayJaccardIndex::executeImpl x")
    assert {"parsequotakey", "quota_key_parser", "arrayjaccardindex", "executeimpl"} <= keep
    assert "x" not in keep
    # a fiddle / commit hash splits like an identifier but is not one
    assert qt.identifier_forms("see 98b4bdfa-7c05008562fe") == frozenset()


def test_noise_tokens_are_numbers_and_hash_fragments() -> None:
    assert (
        qt.is_noise_token("98b4") and qt.is_noise_token("7c05008562fe") and qt.is_noise_token("256")
    )
    assert not qt.is_noise_token("index") and not qt.is_noise_token("bdfa")  # letters only: a word
    assert not qt.is_noise_token("aes256") and not qt.is_noise_token("u64")


def test_strip_non_terms_drops_urls_numbers_and_whole_hashes() -> None:
    text = (
        "the culprit is in https://github.com/o/r/blob/2eed554c3fd75dae1beade79f357ffd18d3c4fdf"
        "/db/query.py#L673 since commit 2EED554C3FD75DAE with id 1290012 in aes256 and deadbeef"
    )
    tokens = code_tokens(qt.strip_non_terms(text))
    # the address goes whole: host, route words, hash pieces and the line anchor
    assert {"https", "github", "blob", "query", "py", "l673"}.isdisjoint(tokens)
    # a hash goes before the tokenizer splits it: no letter-only piece of it is left
    assert {"eed", "fd", "dae", "2eed554c3fd75dae", "1290012"}.isdisjoint(tokens)
    assert tokens == [
        *("the", "culprit", "is", "in", "since", "commit", "with", "id"),
        *("aes256", "aes", "256", "and", "deadbeef"),
    ]
    q = "where SETTINGS secret values are masked"
    assert qt.strip_non_terms(q) == q


def test_select_terms_keeps_the_opening_and_identifiers_not_later_prose() -> None:
    tokens = [
        *("jaccard", "index", "wrong", "the"),  # the opening
        *("culprit", "arrays", "98b4", "unknownword", "parsequotakey", "newname", "or"),
    ]
    df = {
        "jaccard": 12,
        "index": 9000,
        "wrong": 700,
        "the": 76000,
        "culprit": 1,
        "arrays": 400,
        "98b4": 1,
        "parsequotakey": 3,
        "or": 32000,
    }
    lead = frozenset({"jaccard", "index", "wrong", "the"})
    keep = frozenset({"parsequotakey", "newname"})
    # first-seen order; the opening whole, common words included; the identifiers; no
    # later prose, however rare; an identifier the corpus lacks only where room is left
    assert qt.select_terms(tokens, df, 32, keep, lead) == [
        *("jaccard", "index", "wrong", "the"),
        *("parsequotakey", "newname"),
    ]
    assert qt.select_terms(tokens, df, 5, keep, lead) == [
        *("jaccard", "index", "wrong", "the"),
        "parsequotakey",
    ]
    # past the cap the opening goes first, its rarest words first
    assert qt.select_terms(tokens, df, 2, keep, lead) == ["jaccard", "wrong"]
    # in the opening an identifier goes before a word, and the known before the unknown
    lead2 = lead | {"parsequotakey", "unknownword"}
    assert qt.select_terms(tokens, df, 2, keep, lead2) == ["jaccard", "parsequotakey"]
    assert qt.select_terms(tokens, df, 5, keep, lead2) == [
        *("jaccard", "index", "wrong", "the"),
        "parsequotakey",
    ]
    # identifiers after the opening: the rarest first
    many = ["open", "call_command", "max_length", "check_filterable"]
    dfs = {"open": 50, "call_command": 530, "max_length": 2700, "check_filterable": 5}
    ids = frozenset(many[1:])
    assert qt.select_terms(many, dfs, 3, ids, frozenset({"open"})) == [
        *("open", "call_command", "check_filterable")
    ]
    # the cap is never exceeded
    assert len(qt.select_terms(tokens, df, 1, keep, lead)) == 1


def test_plan_under_the_cap_is_the_raw_input() -> None:
    work: dict[str, int] = {}
    q = "where do we retry uploads in parseQuotaKey"
    p = qt.plan(q, None, 48, work)
    assert not p.active
    assert p.lexical_query == q and p.embed_text == q
    assert p.tokens == code_tokens(q)[:32]
    assert work == {}
    # extra_terms extend the lexical query only, as before
    p2 = qt.plan(q, ["QuotaKey"], 48, work)
    assert p2.lexical_query == q + " QuotaKey" and p2.embed_text == q
    # zero turns the trim off for any length
    p3 = qt.plan(_ISSUE, None, 0, work)
    assert not p3.active and p3.embed_text == _ISSUE and work == {}


def test_plan_over_the_cap_cleans_counts_and_bounds() -> None:
    work: dict[str, int] = {}
    p = qt.plan(_ISSUE, None, 32, work)
    assert p.active and p.tokens_in == len(code_tokens(_ISSUE)) > 32
    assert p.cap == 32 and p.boilerplate_dropped == 6
    assert "company" not in p.all_tokens and "response" not in p.all_tokens
    # the link is no term; the vector text keeps it
    assert {"https", "fiddle", "clickhouse", "com", "bdfa"}.isdisjoint(p.all_tokens)
    assert p.embed_text == qt.strip_boilerplate(_ISSUE)[0] and "fiddle" in p.embed_text
    assert {"arrayjaccardindex", "executeimpl", "parsequotakey"} <= p.keep
    # the opening is the first 16 lexemes, half the list
    assert p.lead == frozenset(p.all_tokens[:16])
    assert "arrayjaccardindex" in p.lead and "lives" not in p.lead
    assert qt.plan(_ISSUE, None, 20, {}).lead == frozenset(p.all_tokens[:10])
    # line breaks do not move it: a greeting or a heading line is not the whole opening
    broken = qt.plan("Hi,\n\n" + _ISSUE.replace(". ", ".\n"), None, 32, {})
    assert broken.lead == frozenset(["hi", *p.all_tokens[:15]])
    # a caller's own terms are kept like the opening
    extra = qt.plan(_ISSUE, ["retry", "QuotaKey"], 32, {})
    assert extra.lead == p.lead | {"retry", "quotakey", "quota", "key"}
    assert extra.all_tokens[-2:] == ["retry", "quotakey"]
    assert work == {
        "query_trimmed_tokens_in": p.tokens_in,
        "query_trimmed_boilerplate_lines_dropped": 6,
    }
    # K never widens the leg's list past its historical bound
    assert qt.plan(_ISSUE, None, 40, {}).cap == 32
    assert qt.plan(_ISSUE, None, 20, {}).cap == 20


def test_embed_text_is_the_first_page() -> None:
    long = " ".join(f"w{i}" for i in range(600))
    assert qt.embed_text_for(long) == " ".join(f"w{i}" for i in range(512))
    assert qt.embed_text_for("short text") == "short text"


# ---- through the service ------------------------------------------------------

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


class _Emb:
    model_name = "m"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [0.1, 0.2, 0.3]


_SHORT = "where do we retry uploads in parseQuotaKey"
# every token of the issue is "known" at a df that makes the common words common
_DF = {t: 5 for t in code_tokens(_ISSUE)}
_DF.update(
    {
        "or": 32000,
        "name": 60000,
        "the": 76000,
        "when": 20000,
        "two": 9000,
        "same": 8000,
        "are": 27000,
        "as": 49000,
        "arguments": 3000,
        "https": 4000,
        "clickhouse": 38000,
        "in": 70000,
        "and": 60000,
        "is": 75000,
        "com": 5000,
        "fiddle": 200,
        "function": 15000,
        "lives": 900,
        "unrelated": 300,
    }
)


def _run(
    repo: _Repo, query: str, emb: _Emb | None = None, *, max_tokens: int = 48, **kw: Any
) -> Any:
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", "off"),
        patch.object(settings, "code_search_query_max_tokens", max_tokens),
    ):
        return asyncio.run(cs.search(repo, "o", "n", query=query, top_k=3, embedder=emb, **kw))


def test_a_short_question_is_byte_identical_with_the_trim_on_or_off() -> None:
    on_repo, off_repo = _Repo(_DF), _Repo(_DF)
    on_emb, off_emb = _Emb(), _Emb()
    # two computed searches compared read for read: the corpus-statistics
    # cache would serve the second one's, so bypass it
    with patch.object(settings, "code_corpus_stats_ttl_s", 0):
        on = _run(on_repo, _SHORT, on_emb, max_tokens=48)
        off = _run(off_repo, _SHORT, off_emb, max_tokens=0)
    expected = code_tokens(_SHORT)
    assert on_repo.stats_tokens == off_repo.stats_tokens == expected
    assert on_repo.scan_tokens == off_repo.scan_tokens == expected
    assert on_repo.scan_idfs == off_repo.scan_idfs
    assert on_emb.texts == off_emb.texts == [_SHORT]
    for res in (on, off):
        assert not any(k.startswith("query_trimmed") for k in res.retrieval_work)
        assert "query_trimmed" not in res.score_explanation


def test_a_long_question_asks_with_its_opening_its_identifiers_and_its_cleaned_page() -> None:
    repo, emb = _Repo(_DF), _Emb()
    res = _run(repo, _ISSUE, emb, max_tokens=32)
    cleaned = qt.strip_boilerplate(_ISSUE)[0]
    all_tokens = code_tokens(qt.strip_non_terms(cleaned))
    # the stats read probes every token of the cleaned question: no heading, no link
    assert repo.stats_tokens == all_tokens
    assert "company" not in repo.stats_tokens and "response" not in repo.stats_tokens
    assert "fiddle" not in repo.stats_tokens and "bdfa" not in repo.stats_tokens
    # the scan keeps the opening, common words included, and the identifiers after it
    assert repo.scan_tokens == [
        *("xai", "ai", "jaccard", "index", "calculated", "incorrectly", "when", "two"),
        *("same", "arrays", "are", "passed", "as", "arguments", "select"),
        *("arrayjaccardindex", "executeimpl", "parsequotakey"),
    ]
    assert len(repo.scan_idfs) == 18
    # no prose after the opening, however rare
    assert {"lives", "unrelated", "function", "execute", "quota"}.isdisjoint(repo.scan_tokens)
    # the vector leg embedded the cleaned text
    assert emb.texts == [cleaned]
    w = res.retrieval_work
    assert w["query_trimmed_tokens_in"] == len(code_tokens(_ISSUE))
    assert w["query_trimmed_tokens_used"] == 18
    assert w["query_trimmed_boilerplate_lines_dropped"] == 6
    assert (
        f"query_trimmed(tokens_in={w['query_trimmed_tokens_in']}, tokens_used=18, "
        "boilerplate_lines_dropped=6)"
    ) in res.score_explanation


def test_the_setting_drives_the_trim_and_zero_turns_it_off() -> None:
    n_in = len(code_tokens(_ISSUE))
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", "off"),
        patch.object(settings, "code_search_query_max_tokens", n_in - 1),
    ):
        on_repo = _Repo(_DF)
        on = asyncio.run(
            cs.search(
                on_repo,
                "o",
                "n",
                query=_ISSUE,
                top_k=3,
                embedder=None,
                legs="lexical",
            )
        )
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", "off"),
        patch.object(settings, "code_search_query_max_tokens", 0),
    ):
        off_repo = _Repo(_DF)
        off = asyncio.run(
            cs.search(
                off_repo,
                "o",
                "n",
                query=_ISSUE,
                top_k=3,
                embedder=None,
                legs="lexical",
            )
        )
    assert on.retrieval_work["query_trimmed_tokens_used"] == 18
    assert "query_trimmed_tokens_in" not in off.retrieval_work
    assert off_repo.scan_tokens == code_tokens(_ISSUE)[:32]
    assert off_repo.scan_tokens[0] == "company"  # the reading-order list, as before
    # a cap above the question's length leaves it alone too
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", "off"),
        patch.object(settings, "code_search_query_max_tokens", 512),
    ):
        wide_repo = _Repo(_DF)
        asyncio.run(
            cs.search(
                wide_repo,
                "o",
                "n",
                query=_ISSUE,
                top_k=3,
                embedder=None,
                legs="lexical",
            )
        )
    assert wide_repo.scan_tokens == code_tokens(_ISSUE)[:32]
