"""A long question is asked with its identifiers, the words it repeats and its first
page: server/code/domain/index/query_trim.py and its seam in code_search.search —
byte-identical inputs under the cap, the identifiers and the repeated words at the gate
whatever the question opens with, the cleaned embedding text over it, and the disclosure
counters."""

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


def test_a_hash_is_long_hex_with_digits_and_a_short_code_is_a_name() -> None:
    for h in ("98b4bdfa", "7c05008562fe", "2eed554c3fd75dae1beade79f357ffd18d3c4fdf"):
        assert qt.is_hash(h)
    # a code, a letters-only word, a word around one digit, a number
    for name in ("f401", "e164", "d3d9", "abc123", "deadbeef", "face2face", "aes256", "1290012"):
        assert not qt.is_hash(name)


def test_strip_non_terms_drops_urls_and_whole_hashes_and_keeps_codes() -> None:
    text = (
        "the culprit is in https://github.com/o/r/blob/2eed554c3fd75dae1beade79f357ffd18d3c4fdf"
        "/db/query.py#L673 since commit 2EED554C3FD75DAE with id 1290012 in aes256 and deadbeef"
    )
    tokens = code_tokens(qt.strip_non_terms(text))
    # the address goes whole: host, route words, hash pieces and the line anchor
    assert {"https", "github", "blob", "query", "py", "l673"}.isdisjoint(tokens)
    # a hash goes before the tokenizer splits it: no letter-only piece of it is left
    assert {"eed", "fd", "dae", "2eed554c3fd75dae"}.isdisjoint(tokens)
    assert tokens == [
        *("the", "culprit", "is", "in", "since", "commit", "with", "id", "1290012"),
        *("aes256", "aes", "256", "and", "deadbeef"),
    ]
    codes = "F401 on e164 and d3d9 in face2face returns 404 or 1064"
    assert qt.strip_non_terms(codes) == codes
    q = "where SETTINGS secret values are masked"
    assert qt.strip_non_terms(q) == q


def test_term_counts_count_the_words_that_hold_a_lexeme() -> None:
    said = qt.term_counts("Jaccard index of arrayJaccardIndex, ArrayJaccardIndex::executeImpl")
    assert said["jaccard"] == said["index"] == 3 and said["arrayjaccardindex"] == 2
    assert said["array"] == 2 and said["executeimpl"] == said["of"] == 1


_N = 100_000


def _select(tokens: list[str], df: dict[str, int], k: int = 32, **kw: Any) -> list[str]:
    kw.setdefault("keep", frozenset())
    kw.setdefault("said", {})
    return qt.select_terms(tokens, df, _N, k, **kw)


def test_select_terms_keeps_identifiers_and_repeated_words_before_the_others() -> None:
    tokens = [
        *("hi", "team", "looking", "the", "paginator", "reports", "pages", "model"),
        *("culprit", "1064", "unknownword", "parsequotakey", "newname", "or"),
    ]
    df = dict.fromkeys(tokens, 40) | {
        "the": 76_000,
        "model": 19_000,
        "or": 6_000,
        "culprit": 1,
        "parsequotakey": 3,
        "unknownword": 0,
        "newname": 0,
    }
    keep = frozenset({"parsequotakey", "newname"})
    said = {"paginator": 3, "pages": 2, "the": 9, "model": 4, "or": 5}
    # first-seen order. Never a function word (`the`, `or`), however often it is said,
    # nor a word most chunks hold (`model`); a number and a name the corpus lacks only
    # where room is left
    assert _select(tokens, df, keep=keep, said=said) == [
        *("hi", "team", "looking", "paginator", "reports", "pages"),
        *("culprit", "1064", "unknownword", "parsequotakey", "newname"),
    ]
    # the words take half the list: the repeated ones first, wherever they stand, then
    # the others in reading order; the identifiers take the rest
    assert _select(tokens, df, 8, keep=keep, said=said) == [
        *("hi", "team", "paginator", "pages", "parsequotakey", "newname"),
    ]
    assert _select(tokens, df, 4, keep=keep, said=said) == [
        *("paginator", "pages", "parsequotakey", "newname"),
    ]
    assert _select(tokens, df, 2, keep=keep, said=said) == ["paginator", "parsequotakey"]
    # past the cap the words go before the identifiers, the rarest first
    many = ["open", "call_command", "max_length", "check_filterable"]
    dfs = {"open": 50, "call_command": 530, "max_length": 2700, "check_filterable": 5}
    ids = frozenset(many[1:])
    assert _select(many, dfs, 3, keep=ids) == ["open", "call_command", "check_filterable"]
    # a caller's own terms are always kept, the commonest included, and go first
    own = frozenset({"model", "or"})
    assert _select(tokens, df, 3, keep=keep, said=said, own=own) == ["paginator", "model", "or"]
    # the cap is never exceeded
    assert len(_select(tokens, df, 1, keep=keep, said=said)) == 1


_GREETING = "Hi team, I have been looking at this for a while now and am not sure what is going on."
_PROSE = (
    "The paginator reports more pages than there are when the changelist search spans a "
    "relation. The changelist shows duplicate rows and the paginator counts every duplicate, "
    "so the last pages of the changelist come back empty, which confuses users who walk to "
    "the end and see nothing. The counter under the table still claims hundreds of results, "
    "and the admin offers links that lead nowhere. "
    "Could the paginator count distinct rows instead of duplicate rows?"
)
_SUBJECT = {"paginator", "changelist", "duplicate", "rows", "pages"}


def test_the_subject_is_kept_whatever_the_question_opens_with() -> None:
    assert len(code_tokens(_GREETING)) >= 16
    template = "Is there an existing issue for this? I have searched the existing issues. "
    for opening in ("", _GREETING + " ", template, _GREETING + "\n\n" + template):
        p = qt.plan(opening + _PROSE, None, 48, {})
        assert p.active and p.keep == frozenset()
        df = dict.fromkeys(p.all_tokens, 40)
        kept = _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said, own=p.own)
        assert _SUBJECT <= set(kept), opening


def test_a_question_of_addresses_numbers_and_hashes_still_has_terms() -> None:
    links = " ".join(
        f"https://example.com/o/r/commit/{i:04d}abcdef{i:04d} deadbeef{i:02d}" for i in range(30)
    )
    # nothing but addresses and hashes: they are all the question says
    p = qt.plan(links, None, 48, {})
    assert p.active and p.lexical_query == links and "example" in p.all_tokens
    assert _select(p.all_tokens, {"example": 3, "commit": 900}, p.cap, keep=p.keep, said=p.said)
    # a number is a term when nothing better is left
    numbers = " ".join(f"https://example.com/{i:04d}abcdef{i:04d} {1000 + i}" for i in range(60))
    p = qt.plan(numbers, None, 48, {})
    assert p.all_tokens == [str(1000 + i) for i in range(60)]
    df = dict.fromkeys(p.all_tokens, 7)
    assert _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said) == p.all_tokens[:16]


def test_a_short_code_is_a_subject_word() -> None:
    q = (
        "the linter reports F401 for an import that the module uses only inside a type "
        "checking block and E501 for a long line in the same file and I would like to know "
        "where the rule for F401 decides that a name is unused when the only use is in an "
        "annotation that is evaluated lazily and whether the check for E501 counts the "
        "characters of a trailing comment or not because both reports look wrong to me"
    )
    p = qt.plan(q, None, 48, {})
    assert p.active and {"f401", "e501"} <= p.keep and p.said["f401"] == p.said["e501"] == 2
    df = dict.fromkeys(p.all_tokens, 40)
    kept = _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said)
    assert {"f401", "e501"} <= set(kept)


def test_a_term_list_is_kept_whole_and_cut_to_its_rarest() -> None:
    # eight names of six humps each: 56 lexemes
    terms = ["".join(f"{a}{b}part".capitalize() for b in "abcdef") for a in "abcdefgh"]
    p = qt.plan("", terms, 48, {})
    assert p.active and len(p.all_tokens) == 56 and p.cap == 32
    assert p.own == frozenset(p.all_tokens) and p.keep == {t.lower() for t in terms}
    df = dict.fromkeys(p.all_tokens, 40) | {"aapart": 90_000, "abpart": 80_000, "hfpart": 1}
    kept = _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said, own=p.own)
    # every name, then the rarest pieces: none is lost for where it stands, and a
    # chosen term is not dropped for being common while there is room
    assert len(kept) == 32 and p.keep <= set(kept)
    assert kept[-1] == "hfpart" and {"aapart", "abpart"}.isdisjoint(kept)
    assert "aapart" in _select(p.all_tokens[:20], df, p.cap, keep=p.keep, said=p.said, own=p.own)
    # under the cap the list is the terms' tokens in order
    short = qt.plan("", terms[:2], 48, {})
    assert not short.active and short.tokens == code_tokens(" ".join(terms[:2]))


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
    # how often the question says each lexeme, as a word or inside an identifier
    assert p.said["jaccard"] == p.said["index"] == 3 and p.said["lives"] == 1
    assert p.own == frozenset()
    # a greeting or a line break moves no count
    broken = qt.plan("Hi,\n\n" + _ISSUE.replace(". ", ".\n"), None, 32, {})
    assert broken.said == p.said | {"hi": 1} and broken.keep == p.keep
    # a caller's own terms are named apart: they are always kept
    extra = qt.plan(_ISSUE, ["retry", "QuotaKey"], 32, {})
    assert extra.own == {"retry", "quotakey", "quota", "key"}
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
        return {t: self.df[t] for t in tokens if t in self.df}, 267_000, 50.0

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


def test_a_long_question_asks_with_its_identifiers_its_repeated_words_and_its_cleaned_page() -> (
    None
):
    repo, emb = _Repo(_DF), _Emb()
    res = _run(repo, _ISSUE, emb, max_tokens=32)
    cleaned = qt.strip_boilerplate(_ISSUE)[0]
    all_tokens = code_tokens(qt.strip_non_terms(cleaned))
    # the stats read probes every token of the cleaned question: no heading, no link
    assert repo.stats_tokens == all_tokens
    assert "company" not in repo.stats_tokens and "response" not in repo.stats_tokens
    assert "fiddle" not in repo.stats_tokens and "bdfa" not in repo.stats_tokens
    # the scan keeps the identifiers and the words' half of the list: `jaccard`, `index`
    # and `array`, which the question repeats, then the others in reading order
    assert repo.scan_tokens == [
        *("xai", "ai", "jaccard", "index", "calculated", "incorrectly", "two", "same"),
        *("arrays", "passed", "arguments", "select", "arrayjaccardindex", "array"),
        *("function", "lives", "executeimpl", "execute", "impl", "parsequotakey"),
    ]
    assert len(repo.scan_idfs) == 20
    # no function word, and no word said once after the words' share is taken
    assert {"when", "are", "as", "the", "in", "and", "is"}.isdisjoint(repo.scan_tokens)
    assert {"parse", "quota", "key", "unrelated"}.isdisjoint(repo.scan_tokens)
    # the vector leg embedded the cleaned text
    assert emb.texts == [cleaned]
    w = res.retrieval_work
    assert w["query_trimmed_tokens_in"] == len(code_tokens(_ISSUE))
    assert w["query_trimmed_tokens_used"] == 20
    assert w["query_trimmed_boilerplate_lines_dropped"] == 6
    assert (
        f"query_trimmed(tokens_in={w['query_trimmed_tokens_in']}, tokens_used=20, "
        "boilerplate_lines_dropped=6)"
    ) in res.score_explanation


def test_a_greeting_before_a_prose_question_does_not_cost_its_subject() -> None:
    question = _GREETING + "\n\n" + _PROSE
    df = dict.fromkeys(code_tokens(question), 40) | {"the": 76_000, "of": 49_000}
    repo = _Repo(df)
    _run(repo, question, _Emb())
    assert _SUBJECT <= set(repo.scan_tokens)
    assert {"the", "of", "have", "been", "what"}.isdisjoint(repo.scan_tokens)
    assert len(repo.scan_tokens) == 16  # no identifier: the words' share, no more


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
    assert on.retrieval_work["query_trimmed_tokens_used"] == 20
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
