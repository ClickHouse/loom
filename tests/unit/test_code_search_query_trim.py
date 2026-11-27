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
    # nor does a group of an id or a colour; a word and a number is a name
    assert qt.identifier_forms("span 7c05 of e89b-12d3 in #ff00aa") == frozenset()
    assert qt.identifier_forms("F401 on c8 in e2e with ed25519") == {
        *("f401", "c8", "e2e", "ed25519"),
    }


def test_a_hash_is_long_hex_that_mixes_digits_and_a_code_or_a_numbered_name_is_a_name() -> None:
    for h in ("2eed554", "98b4bdfa", "7c05008562fe", "2eed554c3fd75dae1beade79f357ffd18d3c4fdf"):
        assert qt.is_hash(h)
    # a code, a letters-only word, a word around one digit, a number
    for name in ("f401", "e164", "d3d9", "abc123", "deadbeef", "face2face", "aes256", "1290012"):
        assert not qt.is_hash(name)
    # a word and a number, in either order, however long
    for name in ("ed25519", "fade20481024", "1024bad"):
        assert not qt.is_hash(name) and not qt.is_id_piece(name)
    # letters and digits that alternate are a piece of an id at any length
    for piece in ("7c05", "e89b", "12d3", "ff00aa", "1a2b3c", "2eed554"):
        assert qt.is_id_piece(piece)
    for name in ("f401", "e164", "c8", "abc123", "a456", "deadbeef", "1290012", "x86"):
        assert not qt.is_id_piece(name)
    # under the shortest group of a UUID it is a name
    for name in ("e2e", "b2b", "a1b"):
        assert not qt.is_id_piece(name)
    # the name stays in the question, whole and as an identifier
    key = "the ssh-ed25519 key is rejected"
    assert qt.strip_non_terms(key) == key and "ed25519" in qt.identifier_forms(key)


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
    # a UUID goes whole, in either case: none of its groups is left to read as a name
    ids = "request 123e4567-e89b-12d3-a456-426614174000 and 9F8E7D6C-0A1B-4C2D-8E3F-123456789ABC"
    assert code_tokens(qt.strip_non_terms(ids)) == ["request", "and"]
    # five groups of another length are not one
    longer = "abcdefab-cdef-abcd-efab-cdefabcdefabc"
    assert qt.strip_non_terms(longer) == longer
    q = "where SETTINGS secret values are masked"
    assert qt.strip_non_terms(q) == q


def test_term_counts_count_the_words_that_hold_a_lexeme() -> None:
    said = qt.term_counts("Jaccard index of arrayJaccardIndex, ArrayJaccardIndex::executeImpl")
    assert said["jaccard"] == said["index"] == 3 and said["arrayjaccardindex"] == 2
    assert said["array"] == 2 and said["executeimpl"] == 1
    # a function word in prose is not said, however often: it is there at 0
    assert said["of"] == 0


def test_a_function_word_counts_only_where_it_is_written_as_code() -> None:
    def said(text: str) -> dict[str, int]:
        return {t: n for t, n in qt.term_counts(text).items() if t in qt._FUNCTION_WORDS}

    # called, read off an object, alone in backticks, capitalised inside a sentence
    assert said("only() and qs.only and f().only and a[0].only") == {"only": 4, "and": 0}
    assert said("the `only` method, not the `one and only`") == {
        **{"the": 0, "only": 1, "not": 0, "and": 0},
    }
    assert said("it returns None, the WHERE clause is gone") == {
        **{"it": 0, "none": 1, "the": 0, "where": 1, "is": 0},
    }
    # prose: lower case, a sentence's first word, a line's first word, after an ellipsis
    prose = "None of them. Only one is left\nAll the others over there...then some"
    assert set(said(prose).values()) == {0} and "only" in said(prose)
    # inside an identifier it is the identifier that is said
    assert said("order_by and run_from_argv") == {"by": 0, "and": 0, "from": 0}
    # the stated limit: a capitalised one after a heading that lost its line break
    assert said("Description When I save") == {"when": 1}


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
    said = dict.fromkeys(tokens, 1) | {"paginator": 3, "pages": 2, "the": 0, "model": 4, "or": 0}
    # first-seen order, and room for every word
    assert _select(tokens, df, keep=keep, said=said) == tokens
    # only where room is left, and in this order: a word many chunks hold (`model`),
    # a function word the question never writes as code (`the`, `or`), a number and a
    # name the corpus lacks
    assert _select(tokens, df, 24, keep=keep, said=said) == tokens
    assert _select(tokens, df, 20, keep=keep, said=said) == [
        *("hi", "team", "looking", "the", "paginator", "reports", "pages", "model"),
        *("culprit", "parsequotakey", "newname", "or"),
    ]
    assert _select(tokens, df, 16, keep=keep, said=said) == [
        *("hi", "team", "looking", "paginator", "reports", "pages", "model"),
        *("culprit", "parsequotakey", "newname"),
    ]
    assert _select(tokens, df, 14, keep=keep, said=said) == [
        *("hi", "team", "looking", "paginator", "reports", "pages", "culprit"),
        *("parsequotakey", "newname"),
    ]
    # the words take half the list: the repeated ones first, wherever they stand, then
    # the others in reading order, a common one after them however often it is said;
    # the identifiers take the rest
    assert _select(tokens, df, 8, keep=keep, said=said) == [
        *("hi", "team", "paginator", "pages", "parsequotakey", "newname"),
    ]
    assert _select(tokens, df, 4, keep=keep, said=said) == [
        *("paginator", "pages", "parsequotakey", "newname"),
    ]
    assert _select(tokens, df, 2, keep=keep, said=said) == ["paginator", "parsequotakey"]
    # the cap is never exceeded
    assert len(_select(tokens, df, 1, keep=keep, said=said)) == 1


def test_past_the_cap_a_word_said_once_goes_after_the_identifiers() -> None:
    many = ["open", "call_command", "max_length", "check_filterable"]
    dfs = {"open": 50, "call_command": 530, "max_length": 2700, "check_filterable": 5}
    ids = frozenset(many[1:])
    # a word the question says once is as often prose as subject: the identifiers stay
    assert _select(many, dfs, 3, keep=ids, said={"open": 1}) == many[1:]
    # a word it repeats goes before them, and they are cut to the rarest
    assert _select(many, dfs, 3, keep=ids, said={"open": 2}) == [
        *("open", "call_command", "check_filterable"),
    ]
    # a pasted traceback: forty names and sixteen words said once
    names = [f"frame_{i}_call" for i in range(40)]
    words = ["hi", "team", "looking", *(f"word{i}" for i in range(13))]
    tokens = [*words, *names]
    df = dict.fromkeys(words, 40) | {n: 100 + i for i, n in enumerate(names)}
    kept = _select(tokens, df, keep=frozenset(names), said=dict.fromkeys(tokens, 1))
    assert kept == names[:32]
    # the words it repeats keep their slots, the rarest names take the others
    said = dict.fromkeys(tokens, 1) | {"word0": 2, "word1": 3}
    assert _select(tokens, df, keep=frozenset(names), said=said) == [
        *("word0", "word1", *names[:30]),
    ]
    # fewer names than the slots left: the words said once fill them, in reading order
    assert _select(tokens[:36], df, keep=frozenset(names), said=said) == [
        *("hi", "team", "looking", *(f"word{i}" for i in range(9)), *names[:20]),
    ]


def test_a_callers_terms_take_the_slots_the_question_leaves() -> None:
    question = ["hi", "team", "paginator", "reports", "pages", "parsequotakey"]
    terms = ["pagecount", "page", "count", "orderedset", "ordered", "set", "of", "paginator"]
    tokens = [*question, *terms[:-1]]
    df = dict.fromkeys(tokens, 40) | {"set": 19_000, "of": 60_000, "paginator": 9_000}
    keep, named = frozenset({"parsequotakey"}), frozenset({"pagecount", "orderedset"})
    said = dict.fromkeys(question, 1) | {"paginator": 3, "pages": 2}
    own = frozenset(terms)

    def select(k: int, **kw: Any) -> list[str]:
        return _select(tokens, df, k, keep=keep, said=said, **kw)

    # no word to filter: a common piece and a function word are asked for while there is room
    assert select(32, own=own, named=named) == tokens
    # past the cap: the question's terms, the caller's identifiers, then the rarest pieces
    assert select(8, own=own, named=named) == [
        *("hi", "team", "paginator", "pages", "parsequotakey", "pagecount", "page", "orderedset"),
    ]
    # the question is asked with the same known terms, in the same order, with and
    # without them: `paginator` is its word still when a term holds it, and keeps the
    # slot repetition gives it though it is in more chunks than the pieces that are kept
    for k in (2, 4, 5, 8, 32):
        alone = select(k)
        assert [t for t in select(k, own=own, named=named) if t in alone] == alone
    # a name of its own that the corpus lacks scores nothing: a known piece goes first
    lacking = df | {"parsequotakey": 0}
    assert "parsequotakey" in _select(tokens, lacking, 5, keep=keep, said=said)
    assert "parsequotakey" not in _select(
        tokens, lacking, 5, keep=keep, said=said, own=own, named=named
    )
    assert select(2, own=own, named=named) == ["paginator", "parsequotakey"]
    # a lexeme only the terms hold is no word of the question, however rare
    assert "page" not in select(4, own=own, named=named)
    # a question that fills the list alone keeps its identifiers, however common
    names = [f"frame_{i}_call" for i in range(32)]
    full = [*names, "raresymbol"]
    dfs = dict.fromkeys(names, 9_000) | {"raresymbol": 1}
    kw: dict[str, Any] = dict(keep=frozenset(names), said=dict.fromkeys(names, 1))
    rare = frozenset({"raresymbol"})
    assert _select(full, dfs, 32, own=rare, named=rare, **kw) == names
    assert _select(full[8:], dfs, 32, own=rare, named=rare, **kw) == full[8:]


def test_a_function_word_in_prose_waits_for_every_other_word_whatever_its_class() -> None:
    q = (
        "It doesn't work: the form doesn't save and the view doesn't redirect, and I've also "
        "seen that we'll only get all of the rows through the admin after each save, while "
        "both of the others don't show any. Also only some of them are there, so it isn't "
        "just the form. The select_for_update call should also lock the rows until the "
        "transaction ends, and the admin should not show the rows before the lock is released "
        "or after the transaction is rolled back by another request."
    )
    p = qt.plan(q, None, 48, {})
    # repeated or not: a contraction's half, a quantifier, a preposition, a particle
    function = {"doesn", "ve", "ll", "don", "isn", "also", "only", "all", "through", "each"}
    function |= {"both", "any", "some", "after", "while", "until", "before", "just", "another"}
    assert p.active and function <= set(p.all_tokens)
    assert {p.said[t] for t in function} == {0}
    kept = _select(p.all_tokens, dict.fromkeys(p.all_tokens, 40), p.cap, keep=p.keep, said=p.said)
    assert function.isdisjoint(kept)
    # the content words are kept, and a function word inside an identifier is asked for there
    assert {"form", "rows", "admin", "transaction", "select_for_update"} <= set(kept)


def test_a_function_word_written_as_code_is_a_word_of_the_question() -> None:
    filler = (
        "We have looked at this for a while and it is not clear to us what is going on or "
        "where it comes from, so here is what we see on our side after the upgrade of the "
        "project. "
    )
    assert len(code_tokens(filler)) > 16
    cases = {
        "only": (
            "The docs say that only() defers every other column. With a proxy model the "
            "query built by only() still selects the whole row, and only() after "
            "select_related crashes."
        ),
        "none": (
            "The bulk update of the rows returns None, unlike a plain update, which returns "
            "the count of matched rows. The caller cannot tell whether a single row was "
            "matched or skipped when the bulk update returns None."
        ),
    }
    for word, subject in cases.items():
        p = qt.plan(filler + subject, None, 48, {})
        assert p.active and p.said[word] >= 2 and word not in p.keep
        df = dict.fromkeys(p.all_tokens, 40)
        assert word in _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said)
        # the same subject in prose: the word is English there, and waits
        prose = qt.plan(filler + subject.replace("()", "").replace("None", "none"), None, 48, {})
        assert prose.active and prose.said[word] == 0
        df = dict.fromkeys(prose.all_tokens, 40)
        assert word not in _select(
            prose.all_tokens, df, prose.cap, keep=prose.keep, said=prose.said
        )


def test_a_question_of_function_words_alone_still_has_terms() -> None:
    words = sorted(w for w in qt._FUNCTION_WORDS if len(w) > 1)[:60]
    p = qt.plan(" ".join(words), None, 48, {})
    assert p.active and p.keep == frozenset() and set(p.said.values()) == {0}
    df = dict.fromkeys(p.all_tokens, 40)
    # nothing better is left: the leg is asked with the first of them, not with nothing
    assert _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said) == words[:16]


_TRACEBACK = """Saving the changelist form raises an error since the upgrade.
Traceback (most recent call last):
  File "/usr/lib/python3.11/site-packages/django/core/handlers/exception.py", line 55, in inner
    response = get_response(request)
  File "/usr/lib/python3.11/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request, *callback_args, **callback_kwargs)
  File "/usr/lib/python3.11/site-packages/django/contrib/admin/options.py", line 688, in wrapper
    return self.admin_site.admin_view(view)(*args, **kwargs)
  File "/usr/lib/python3.11/site-packages/django/contrib/admin/options.py", line 1889, in changelist_view
    if formset.is_valid():
  File "/usr/lib/python3.11/site-packages/django/forms/formsets.py", line 384, in is_valid
    self.errors
  File "/usr/lib/python3.11/site-packages/django/forms/formsets.py", line 366, in errors
    self.full_clean()
  File "/usr/lib/python3.11/site-packages/django/forms/formsets.py", line 429, in full_clean
    raise ValidationError("ManagementForm data is missing or has been tampered with")
"""


def test_a_traceback_keeps_the_names_its_frames_call_and_repeats_its_format() -> None:
    p = qt.plan(_TRACEBACK, None, 48, {})
    assert p.active
    df = dict.fromkeys(p.all_tokens, 40) | {"django": 60_000, "py": 90_000}
    kept = _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said)
    # every function a frame names is asked for, whole
    frames = {"get_response", "_get_response", "wrapped_callback", "changelist_view"}
    assert frames | {"is_valid", "full_clean", "validationerror", "managementform"} <= set(kept)
    # the stated limit: each frame repeats the words of its format and the directories
    # of its path, and they take 7 of the 16 word slots, before the words the question
    # says once and the ones the later frames repeat
    assert {"file", "line", "usr", "lib", "python", "site", "packages"} <= set(kept)
    assert {"saving", "raises", "upgrade"}.isdisjoint(kept)
    assert {"admin", "forms", "formsets", "options"}.isdisjoint(kept)
    assert len(kept) == 16 + len(p.keep)


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


def test_a_common_word_is_asked_for_when_no_other_word_is_left() -> None:
    p = qt.plan(_PROSE, None, 48, {})
    # five chunks: every word the namespace knows is in more than one chunk in eight
    df = dict.fromkeys(p.all_tokens, 2)
    kept = qt.select_terms(p.all_tokens, df, 5, p.cap, keep=p.keep, said=p.said)
    assert len(kept) == 16 and _SUBJECT <= set(kept)
    # a large namespace where every word of the question is common
    df = dict.fromkeys(p.all_tokens, 30_000)
    assert _SUBJECT <= set(_select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said))
    # a common word waits for the others: sixteen rarer ones take the share
    df = dict.fromkeys(p.all_tokens, 40) | dict.fromkeys(_SUBJECT, 30_000)
    kept = _select(p.all_tokens, df, p.cap, keep=p.keep, said=p.said)
    assert len(kept) == 16 and _SUBJECT.isdisjoint(kept)


def test_a_question_of_addresses_numbers_and_hashes_still_has_terms() -> None:
    links = " ".join(
        f"https://example.com/o/r/commit/{i:04d}abcdef{i:04d} dead{i:02d}beef{i:02d}"
        for i in range(30)
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
    assert p.own == frozenset(p.all_tokens) and p.named == {t.lower() for t in terms}
    assert p.keep == frozenset() and p.said == {}
    df = dict.fromkeys(p.all_tokens, 40) | {"aapart": 90_000, "abpart": 80_000, "hfpart": 1}
    kw: dict[str, Any] = dict(keep=p.keep, said=p.said, own=p.own, named=p.named)
    kept = _select(p.all_tokens, df, p.cap, **kw)
    # every name, then the rarest pieces: none is lost for where it stands, and a
    # chosen term is not dropped for being common while there is room
    assert len(kept) == 32 and p.named <= set(kept)
    assert kept[-1] == "hfpart" and {"aapart", "abpart"}.isdisjoint(kept)
    assert "aapart" in _select(p.all_tokens[:20], df, p.cap, **kw)
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
    # a caller's own terms are named apart, and none is stripped: one shaped like a
    # hash is a term too
    extra = qt.plan(_ISSUE, ["retry", "QuotaKey", "2eed554c3fd75dae", "Jaccard"], 32, {})
    assert {"retry", "quotakey", "quota", "key", "2eed554c3fd75dae", "jaccard"} <= extra.own
    assert extra.own <= set(extra.all_tokens) and extra.named == {"quotakey"}
    assert extra.all_tokens[len(p.all_tokens) :][:3] == ["retry", "quotakey", "2eed554c3fd75dae"]
    # they are no words of the question: its identifiers and its counts are its own
    assert extra.keep == p.keep and extra.said == p.said
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


def test_proposed_terms_do_not_cost_a_long_question_its_own_identifiers() -> None:
    question = _PROSE + " I think get_search_results in ChangeList.get_queryset goes wrong."
    proposed = "QuerySet SqlCompiler execute_sql SelectRelated DistinctOn PageCount".split()
    tokens = code_tokens(question + " " + " ".join(proposed))
    common = {"query": 90_000, "select": 60_000, "set": 120_000, "on": 200_000}
    repo = _Repo(dict.fromkeys(tokens, 40) | common)
    _run(repo, question, _Emb(), extra_terms=proposed)
    scan = set(repo.scan_tokens)
    # what the question spells and what the caller proposes are both asked for, whole
    assert {"get_search_results", "changelist", "get_queryset"} <= scan
    assert {t.lower() for t in proposed} <= scan
    # the pieces of the proposed names take the slots that are left, the rarest first
    assert len(scan) == 32 and {"compiler", "related"} <= scan and common.keys().isdisjoint(scan)


def test_a_proposed_term_that_repeats_a_word_of_the_question_does_not_cost_it() -> None:
    question = _PROSE + " I think get_search_results in ChangeList.get_queryset goes wrong."
    # the proposer echoes the subject: `paginator`, `rows` and `pages` are words of the
    # question, and each is in more chunks than any piece of the other names
    proposed = (
        "Paginator DuplicateRows page_count_cache SqlCompilerBase execute_raw_sql SelectRelatedField"
    ).split()
    tokens = code_tokens(question + " " + " ".join(proposed))
    echoed = {"paginator": 30_000, "rows": 25_000, "pages": 20_000}
    df = dict.fromkeys(tokens, 40) | echoed
    alone, repo = _Repo(df), _Repo(df)
    _run(alone, question, _Emb())
    _run(repo, question, _Emb(), extra_terms=proposed)
    assert len(alone.scan_tokens) < 32 == len(repo.scan_tokens)
    # more lexemes than slots: some piece of the proposed names is cut
    cut = set(code_tokens(" ".join(proposed))) - set(repo.scan_tokens)
    assert cut and echoed.keys().isdisjoint(cut)
    # the question's own list is asked for whole, in its order, beside the proposed names
    assert [t for t in repo.scan_tokens if t in alone.scan_tokens] == alone.scan_tokens
    assert echoed.keys() <= set(repo.scan_tokens)
    assert {t.lower() for t in proposed} <= set(repo.scan_tokens)


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
