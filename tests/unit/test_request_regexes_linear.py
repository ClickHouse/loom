"""The regexes the API runs on request text read a long run in linear time.

Each case searches the way its caller does, on a repeat of the unit that
made the pattern quadratic: from n to 4n a linear read takes about 4 times
as long and a quadratic one 16 times. The pattern is read from its module at
call time, so the case measures what the server runs."""

from __future__ import annotations

import random
import re
import signal
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.code.app import citations as citations_app
from server.code.app import symbol_context
from server.code.app.common import repo_relative
from server.code.app.index import review_brief, sql_names
from server.code.domain import facets
from server.code.domain.index import citations, query_trim
from server.identity.app import scim_groups
from server.insights.app import mentor_rules
from server.knowledge.domain import exact_keys, transcripts
from server.memory.app import observe_replay, value_updates
from server.memory.app.ask import identifiers, prompts
from server.memory.app.loom_search import tracker_leg
from server.memory.domain import (
    activity_facts,
    ask_threshold,
    conflicts,
    files,
    forget_requests,
    passage_trim,
    precedents,
    premise_facts,
    query_nodes,
    query_planner,
    retrieval_calibration,
)
from server.storage import frontend_assets
from server.tracker.api.models import (
    MAX_FRAME_CHARS,
    TrackerDuplicateCheckInput,
    TrackerSimilarInput,
    within_item_bound,
)
from server.tracker.domain import bisect, defect_answer, similar

# A run this long takes a quadratic pattern well past the 8x bound, and the
# whole file stays within a few seconds.
N = 4_000
# Below this, a timing is noise.
_FLOOR_S = 0.002


def _finditer(module: Any, name: str) -> Callable[[str], object]:
    return lambda s: list(getattr(module, name).finditer(s))


def _sub(module: Any, name: str, repl: str = " ", count: int = 0) -> Callable[[str], object]:
    return lambda s: getattr(module, name).sub(repl, s, count=count)


def _search(module: Any, name: str) -> Callable[[str], object]:
    return lambda s: getattr(module, name).search(s)


def _match(module: Any, name: str) -> Callable[[str], object]:
    return lambda s: getattr(module, name).match(s)


def _findall(module: Any, name: str) -> Callable[[str], object]:
    return lambda s: getattr(module, name).findall(s)


def _then(read: Callable[[str], object], tail: str) -> Callable[[str], object]:
    """`read` on the run followed by `tail`: the text that makes the match fail late."""
    return lambda s: read(s + tail)


def _amounts(s: str) -> object:
    row = SimpleNamespace(content_excerpt=s, memory_key="k", valid_at="")
    return prompts.amount_candidates([row], money=None)


def _cue(s: str) -> object:
    return transcripts.parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n" + s + "\n")


def _voice_cue(s: str) -> object:
    # a word after the blanks, so the line's strip keeps them
    return _cue(s + "x")


def _forget(s: str) -> object:
    return forget_requests.forget_sentences([{"role": "user", "content": s}])


def _pytest_ids(s: str) -> object:
    return list(retrieval_calibration._PYTEST_ID_RE.finditer(s))


def _test_identifiers(s: str) -> object:
    return [list(p.finditer(s)) for p in retrieval_calibration._TEST_IDENTIFIER_PATTERNS]


# (pattern, call as its caller makes it, text before the repeat, repeated unit)
CASES: list[tuple[str, Callable[[str], object], str, str]] = [
    ("facets._FILE_PATH_RE", _finditer(facets, "_FILE_PATH_RE"), "", "@"),
    ("facets._BARE_FILE_RE", _finditer(facets, "_BARE_FILE_RE"), "", "@"),
    ("files._URL_RE", _sub(files, "_URL_RE"), "", "a"),
    ("citations._PATH_RE", _finditer(citations, "_PATH_RE"), "", "@"),
    ("citations._LOC_RE", _finditer(citations, "_LOC_RE"), "", "+"),
    ("citations._BARE_LOC_RE", _finditer(citations, "_BARE_LOC_RE"), "", "a+"),
    ("citations diff prefixes", _then(citations.extract, "x.py"), "", "a/"),
    ("citations fenced citations", citations.extract, "", "```a``` a/b.py "),
    ("query_trim._QUALIFIED_RE", _finditer(query_trim, "_QUALIFIED_RE"), "", "_"),
    ("similar.TRACE_SYMBOL_RE", _finditer(similar, "TRACE_SYMBOL_RE"), "", "a_"),
    ("similar.TRACE_FILELINE_RE", _finditer(similar, "TRACE_FILELINE_RE"), "", "a/"),
    ("similar._TRACE_LINE_RE words", _match(similar, "_TRACE_LINE_RE"), "", "a::a"),
    ("similar._TRACE_LINE_RE blanks", _match(similar, "_TRACE_LINE_RE"), "", " "),
    ("similar._TRACE_LINE_RE frame", _match(similar, "_TRACE_LINE_RE"), "#1 ", "a"),
    ("prompts._AMOUNT_RE digits", _finditer(prompts, "_AMOUNT_RE"), "", "1"),
    ("prompts._AMOUNT_RE commas", _finditer(prompts, "_AMOUNT_RE"), "", "1,,"),
    ("conflicts._PARENTHETICAL_RE", _sub(conflicts, "_PARENTHETICAL_RE", ""), "", "\t"),
    ("value_updates._WORD_RE", _findall(value_updates, "_WORD_RE"), "", "1"),
    ("identifiers._TECH_FILE_RE", _finditer(identifiers, "_TECH_FILE_RE"), "", "a-"),
    ("scim_groups._EQ", _sub(scim_groups, "_EQ", " eq ", 1), "", " "),
    ("observe_replay._RESULT_LINE", _search(observe_replay, "_RESULT_LINE"), "", "\n"),
    ("activity_facts._PATHLIKE_RE word", _sub(activity_facts, "_PATHLIKE_RE"), "", "é"),
    ("activity_facts._PATHLIKE_RE ticks", _sub(activity_facts, "_PATHLIKE_RE"), "", "a`"),
    ("activity_facts._TOOL_SUBJECT_RE dashes", _match(activity_facts, "_TOOL_SUBJECT_RE"), "", "-"),
    (
        "activity_facts._TOOL_SUBJECT_RE topic",
        _match(activity_facts, "_TOOL_SUBJECT_RE"),
        "the search for x about",
        " ",
    ),
    ("query_planner._MIXED_RE", _finditer(query_planner, "_MIXED_RE"), "", "a-"),
    ("sql_names._STATED_CLASS_RE", _search(sql_names, "_STATED_CLASS_RE"), "", "a"),
    ("sql_names._ANY_REG_RE prefixed", _finditer(sql_names, "_ANY_REG_RE"), "", "_register"),
    ("review_brief._DECL_RE prefixed", _finditer(review_brief, "_DECL_RE"), "", "_DECLARE"),
    (
        "review_brief._OBSOLETE_RE prefixed",
        _finditer(review_brief, "_OBSOLETE_RE"),
        "",
        "_MAKE_OBSOLETE",
    ),
    (
        "sql_names._STATED_CLASS_RE blanks",
        _then(_search(sql_names, "_STATED_CLASS_RE"), "b::"),
        "a",
        " ",
    ),
    # One word of humps: the old patterns were exponential in its length.
    (
        "identifiers._INJECT_HANDLE_RE",
        _then(_finditer(identifiers, "_INJECT_HANDLE_RE"), "_"),
        "Aa",
        "A",
    ),
    (
        "identifiers._TECH_PASCAL_RE",
        _then(_finditer(identifiers, "_TECH_PASCAL_RE"), "_"),
        "Aa",
        "A",
    ),
    ("identifiers._INJECT_FILELINE_RE", _finditer(identifiers, "_INJECT_FILELINE_RE"), "", "a/"),
    ("retrieval_calibration pytest ids", _pytest_ids, "", "a-"),
    ("retrieval_calibration pytest params", _test_identifiers, "", "a.py::b["),
    ("retrieval_calibration test_ params", _test_identifiers, "", "test_abc["),
    (
        "activity_facts._FAILED_ATTEMPT_RE",
        _search(activity_facts, "_FAILED_ATTEMPT_RE"),
        "the user ",
        "call ",
    ),
    (
        "citations._BLOB_URL_RE",
        _finditer(citations, "_BLOB_URL_RE"),
        "https://github.com/o/r/blob/",
        "a/",
    ),
    (
        "defect_answer._SEGMENT_SPLIT_RE",
        _then(lambda s: defect_answer._SEGMENT_SPLIT_RE.split(s), "b"),
        "a",
        " ",
    ),
    ("passage_trim._ENUM_LINE_RE", _findall(passage_trim, "_ENUM_LINE_RE"), "", "\n"),
    ("precedents._TRAIL_RE", _then(_sub(precedents, "_TRAIL_RE", ""), "x"), "", "?"),
    ("query_nodes._TRAIL_PUNCT", _then(_sub(query_nodes, "_TRAIL_PUNCT", ""), "x"), "", "?"),
    ("ask_threshold._NUM_RE", _then(_finditer(ask_threshold, "_NUM_RE"), "x"), "", " "),
    (
        "ask_threshold._PART_SPLIT_RE",
        _then(lambda s: ask_threshold._PART_SPLIT_RE.split(s), "x"),
        "",
        " ",
    ),
    ("citations._NAME_RE", _then(_finditer(citations, "_NAME_RE"), "::"), "a", "::~a"),
    # "no wait" parsed two ways per repeat was exponential in the repeats.
    (
        "forget_requests._FORGET_MSG_RE",
        _then(_match(forget_requests, "_FORGET_MSG_RE"), "hello"),
        "",
        "no wait ",
    ),
    ("exact_keys._TERM", exact_keys.query_keys, "", "a-"),
    (
        "mentor_rules assertion line",
        mentor_rules.looks_like_stack_trace,
        "#3 0x55d1 in DB::x\n" + "x" * 80,
        "Assertion ",
    ),
    ("transcripts._TAG", _cue, "", "<"),
    ("transcripts._VOICE", _voice_cue, "<v.", "\t"),
    ("mentor_rules._TRACE_LINE_RES blanks", mentor_rules.looks_like_stack_trace, "x" * 80, "\n"),
    ("mentor_rules._TRACE_LINE_RES frame", mentor_rules.looks_like_stack_trace, "1. ", "a"),
    (
        "mentor_rules._TRACE_LINE_RES Python frame",
        mentor_rules.looks_like_stack_trace,
        "x" * 80 + "\n",
        'File "a\n',
    ),
    # Per-text work done once, not once per match.
    ("facets.extract_code_facets hex runs", facets.extract_code_facets, "", "deadbee "),
    ("prompts.amount_candidates", _amounts, "", "Ab 1 km "),
    ("bisect.report_text unclosed comments", bisect.report_text, "", "<!--"),
    ("bisect.report_text unpaired markers", bisect.report_text, "", "<!-- a -->"),
    ("bisect.report_text unpaired start markers", bisect.report_text, "", "<!-- a:start -->"),
    ("forget_requests.forget_sentences", _forget, "", "forget that I like jazz. "),
]


# Longer than any linear read takes on a loaded runner; an exponential
# pattern would otherwise hang the job instead of failing the case.
_CAP_S = 10.0


def _timed_out(signum: int, frame: object) -> None:
    raise TimeoutError(f"one read took over {_CAP_S}s")


def _fastest_seconds(read: Callable[[str], object], text: str) -> float:
    """The fastest of three reads in CPU time: another process holding the CPU does not count."""
    times = []
    previous = signal.signal(signal.SIGALRM, _timed_out)
    try:
        for _ in range(3):
            signal.setitimer(signal.ITIMER_REAL, _CAP_S)
            started = time.process_time()
            read(text)
            times.append(time.process_time() - started)
            signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return min(times)


@pytest.mark.parametrize(
    ("read", "head", "unit"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_a_long_run_is_read_in_linear_time(
    read: Callable[[str], object], head: str, unit: str
) -> None:
    short = head + unit * (N // len(unit))
    long = head + unit * (4 * N // len(unit))
    assert _fastest_seconds(read, long) < 8 * _fastest_seconds(read, short) + _FLOOR_S


@pytest.mark.parametrize(
    "read",
    [files.normalize_file_ref, citations.extract, repo_relative, symbol_context.normalize_path],
    ids=["files", "citations", "repo_relative", "symbol_context"],
)
def test_a_long_run_of_dot_slashes_is_stepped_past(read: Callable[[str], object]) -> None:
    # a slice per `./` copies the rest of the path each time; from n to 8n
    # that grows about 64 times, a single pass about 8
    short = "./" * 12_000 + "x.py"
    long = "./" * 96_000 + "x.py"  # under the 200 KB citations cap
    assert _fastest_seconds(read, long) < 24 * _fastest_seconds(read, short) + _FLOOR_S


def test_an_asset_name_longer_than_a_file_name_is_not_hashed() -> None:
    stem = "a" * (255 - len("-0123abcd.js"))
    assert frontend_assets.is_hashed_asset(f"assets/{stem}-0123abcd.js")
    assert not frontend_assets.is_hashed_asset(f"assets/{stem}x-0123abcd.js")
    # the directory does not count toward the name
    assert frontend_assets.is_hashed_asset("x" * 1000 + "/index-0123abcd.js")


def test_a_forget_sentence_is_read_in_time_linear_in_its_blank_runs() -> None:
    # the cap bounds each sentence, not how many a message holds, so each
    # must cost its length: from a quarter of the cap to all of it, about 4x
    def message(blanks: int) -> str:
        return ("forget jazz" + " " * blanks + "music. ") * 200

    def read(text: str) -> object:
        msgs = [{"role": "user", "content": text}]
        return forget_requests.forget_requests_from_messages(msgs), _forget(text)

    assert _fastest_seconds(read, message(380)) < 8 * _fastest_seconds(read, message(95)) + _FLOOR_S


def test_a_window_states_at_most_a_bounded_number_of_premises() -> None:
    turns = [
        {
            "role": "assistant",
            "content": " ".join(f"Since you enjoy qa{i}x{j} qb{i}x{j}." for j in range(20)),
        }
        for i in range(10)
    ]
    out = premise_facts.premise_facts_from_messages(turns, [])
    assert len(out) == premise_facts.MAX_PREMISES


def test_forget_targets_reads_the_batch_once_not_once_per_request() -> None:
    def read(n: int) -> Callable[[str], object]:
        facts = [{"key": "", "value": f"jazz{i}", "forget": True} for i in range(n)]
        return lambda _: forget_requests.forget_targets(facts, [], set())

    assert _fastest_seconds(read(8_000), "") < 8 * _fastest_seconds(read(2_000), "") + _FLOOR_S


def test_a_deeply_qualified_name_is_retried_under_its_last_qualifiers_only() -> None:
    assert citations_app._shorter("A::B::C") == ["B::C", "C"]
    deep = citations_app._shorter("::".join(["a"] * 60_000))
    assert len(deep) == citations_app.MAX_QUALIFIER_RETRIES
    assert deep[-1] == "a"


def test_a_forget_sentence_past_the_cap_is_not_read() -> None:
    def sentence(pad: int) -> str:
        return "forget that I prefer Neovim" + "x" * pad + ", I switched to VS Code"

    at_cap = sentence(forget_requests.MAX_FORGET_SENTENCE_CHARS - len(sentence(0)))
    over = at_cap + "x"
    assert forget_requests.forget_sentences([{"role": "user", "content": at_cap}]) == {
        0: "I switched to VS Code"
    }
    assert forget_requests.forget_sentences([{"role": "user", "content": over}]) == {}

    # a request too long to read is still not a sentence the message says besides it
    long_request = (
        "forget my old address on Elm Street" + " x" * forget_requests.MAX_FORGET_SENTENCE_CHARS
    )
    message = f"forget that I like jazz. {long_request}. I live on Oak Road now."
    assert forget_requests.forget_sentences([{"role": "user", "content": message}]) == {
        0: "I live on Oak Road now."
    }


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (TrackerSimilarInput, "frames"),
        (TrackerDuplicateCheckInput, "frames"),
        (TrackerDuplicateCheckInput, "symbols"),
        (TrackerDuplicateCheckInput, "paths"),
    ],
)
def test_a_frame_line_is_bounded(model: type[Any], field: str) -> None:
    model(namespace="t", **{field: ["a" * MAX_FRAME_CHARS]})
    with pytest.raises(ValidationError):
        model(namespace="t", **{field: ["a" * (MAX_FRAME_CHARS + 1)]})


def test_the_search_tracker_leg_drops_a_frame_the_check_would_refuse() -> None:
    long_frame = "DB::" + "a" * MAX_FRAME_CHARS
    shape = tracker_leg.defect_report_shape(
        f"LOGICAL_ERROR in {long_frame} and DB::Foo::bar — already reported?"
    )
    assert shape is not None and shape["frames"] == ["DB::Foo::bar"]
    TrackerDuplicateCheckInput(namespace="t", frames=shape["frames"])


def test_a_frame_the_check_would_refuse_is_dropped_before_it() -> None:
    frames = ["DB::" + "a" * MAX_FRAME_CHARS, "DB::Foo::bar"]
    assert within_item_bound(frames) == ["DB::Foo::bar"]
    TrackerDuplicateCheckInput(namespace="t", frames=within_item_bound(frames))


def _without(pattern: str, guard: str) -> str:
    assert pattern.count(guard) == 1, "the rewrite no longer carries its guard"
    return pattern.replace(guard, "")


# The rewrites that changed most, against the patterns they replaced: the same
# reads on short random text drawn from the characters each one turns on.
_ORIGINAL = {
    "TRACE_SYMBOL_RE": re.compile(r"(?:[A-Za-z_~][\w~]*::)+[A-Za-z_~][\w~]*"),
    "_TRACE_LINE_RE": re.compile(
        r"^\s*(?:(?:#\d+|\d+\.)\s+.*[A-Za-z_]\w*::[A-Za-z_~]"
        r"|.*[A-Za-z_]\w*::[A-Za-z_~]\w*.*\s@\s0x[0-9A-Fa-f]{6,})"
    ),
    "_MIXED_RE": re.compile(
        r"\b(?=[A-Za-z0-9_-]{6,}\b)(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]+\b"
    ),
    "_PATHLIKE_RE": re.compile(r"`[^`]*`|\S*[/\\]\S*|\S+[._-]\S*[._-]\S*|\S+\.\w{1,5}\b"),
    "_TERM": re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*(?:\.[A-Za-z0-9_$-]+)+|[A-Za-z_][A-Za-z0-9_$]*"),
    "_SEGMENT_SPLIT_RE": re.compile(r"(\s+[—–]\s+|\s+-\s+(?=\S)|(?<=[?!])\s+|:\s+(?=\S))"),
    # the number words are long, so the original is the rewrite without its guard
    "_NUM_RE": re.compile(_without(ask_threshold._NUM_RE.pattern, r"(?<!\s)")),
    "_PART_SPLIT_RE": re.compile(
        r"\s*(?:\?|\.)\s*(?:Additionally|Also|In addition|And)[,:]?\s+|;\s+", re.I
    ),
    "_AMOUNT_RE": re.compile(
        r"(?:\$|€|£)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:dollars?|usd|followers?|"
        r"miles|km|kg|lbs?|hours?|hrs|minutes?|mins|steps|calories|points|views|subscribers)\b",
        re.IGNORECASE,
    ),
    "_REPLACEMENT_CUT_RE": re.compile(
        r"\s+[—–]\s*|\s+-\s+|\s*;\s*|"
        r",\s*(?=(?:i|i'm|i've|i'd|we|we're|we've|it|it's|its|my|our|now|they|he|she|"
        r"that's|the\s+new|instead|but|and\s+(?:i|we|now))\b)",
        re.IGNORECASE,
    ),
    "_FAILED_ATTEMPT_RE": re.compile(
        r"\b(?:attempt|call|request|sequence|registration)\b[^.;]*?"
        r"\b(?:refused|could\s+not|couldn't|failed|was\s+rejected)\b",
        re.IGNORECASE,
    ),
}


def _texts(pieces: list[str], seed: int) -> list[str]:
    rnd = random.Random(seed)
    return ["".join(rnd.choice(pieces) for _ in range(rnd.randint(1, 16))) for _ in range(2000)]


def _names(p: re.Pattern[str], s: str, group: int) -> list[tuple[tuple[int, int], str]]:
    return [(m.span(group), m.group(group)) for m in p.finditer(s)]


@pytest.mark.parametrize(
    ("name", "new", "read_old", "read_new", "pieces"),
    [
        (
            "TRACE_SYMBOL_RE",
            similar.TRACE_SYMBOL_RE,
            lambda p, s: _names(p, s, 0),
            lambda p, s: _names(p, s, 1),
            ["a", "Z", "_", "~", "::", ":", "9", " ", "(", "é", "DB", "Foo"],
        ),
        (
            "_TRACE_LINE_RE",
            similar._TRACE_LINE_RE,
            lambda p, s: bool(p.match(s)),
            lambda p, s: bool(p.match(s)),
            ["#1 ", "2. ", " ", "\t", "a", "~", "::", "DB::Foo", "9", " @ 0x", "1c4652", "é", "@"],
        ),
        (
            "_MIXED_RE",
            query_planner._MIXED_RE,
            lambda p, s: _names(p, s, 0),
            lambda p, s: _names(p, s, 1),
            ["a", "1", "-", "_", " ", "é", ".", "Zb9", "ab12", "--"],
        ),
        (
            "_PATHLIKE_RE",
            activity_facts._PATHLIKE_RE,
            lambda p, s: p.sub(" ", s),
            lambda p, s: p.sub(" ", s),
            ["a", "/", ".", "_", "-", " ", "`", "\\", "é", "!", "x.py", "`cmd`"],
        ),
        (
            "_TERM",
            exact_keys._TERM,
            lambda p, s: [t.rstrip("-") for t in p.findall(s)],
            lambda p, s: [(dotted or word).rstrip("-") for dotted, word in p.findall(s)],
            ["a", "Z", "_", "$", "-", ".", "9", " ", "é", "x.y"],
        ),
        (
            "_SEGMENT_SPLIT_RE",
            defect_answer._SEGMENT_SPLIT_RE,
            lambda p, s: p.split(s),
            lambda p, s: p.split(s),
            [" ", "\t", "—", "–", "-", "?", "!", ":", "a", "b "],
        ),
        (
            "_NUM_RE",
            ask_threshold._NUM_RE,
            lambda p, s: [m.group("num", "tens") for m in p.finditer(s)],
            lambda p, s: [m.group("num", "tens") for m in p.finditer(s)],
            [" ", "about", "~", "12", "3.5", "twenty", "-", "one", "a", "x"],
        ),
        (
            "_PART_SPLIT_RE",
            ask_threshold._PART_SPLIT_RE,
            lambda p, s: p.split(s),
            lambda p, s: p.split(s),
            [" ", "?", ".", ";", "Also", "And", ",", "x", "In addition", ":"],
        ),
        (
            "_AMOUNT_RE",
            prompts._AMOUNT_RE,
            lambda p, s: [m.groups() for m in p.finditer(s)],
            lambda p, s: [m.groups() for m in p.finditer(s)],
            ["$", "1", ",", ".5", " ", "km", "usd", "x", "€", "9"],
        ),
        (
            "_REPLACEMENT_CUT_RE",
            forget_requests._REPLACEMENT_CUT_RE,
            lambda p, s: p.split(s, maxsplit=1),
            lambda p, s: p.split(s, maxsplit=1),
            [" ", "\t", "—", "-", ";", ",", "I", "x", " now", "we"],
        ),
        (
            "_FAILED_ATTEMPT_RE",
            activity_facts._FAILED_ATTEMPT_RE,
            lambda p, s: bool(p.search(s)),
            lambda p, s: bool(p.search(s)),
            ["call ", "request ", "failed", ".", ";", " ", "x", "could not", "refused"],
        ),
    ],
    ids=list(_ORIGINAL),
)
def test_a_rewrite_reads_what_the_original_read(
    name: str,
    new: re.Pattern[str],
    read_old: Callable[[re.Pattern[str], str], object],
    read_new: Callable[[re.Pattern[str], str], object],
    pieces: list[str],
) -> None:
    old = _ORIGINAL[name]
    for s in _texts(pieces, seed=len(name)):
        assert read_new(new, s) == read_old(old, s), repr(s)


def test_a_registration_call_on_a_prefixed_name_is_read() -> None:
    text = 'self._register("arrayFold"); auto_register<T>("sumMap"); registerFunction("abs")'
    assert sql_names._ANY_REG_RE.findall(text) == ["arrayFold", "sumMap", "abs"]
    assert sql_names._shape_re("arrayFold").search(text)


def test_a_settings_macro_with_a_prefix_is_read() -> None:
    text = "M_DECLARE(UInt64, max_threads, 0) X_MAKE_OBSOLETE(M, Bool, old_flag, 0)"
    assert review_brief._DECL_RE.findall(text) == ["max_threads"]
    assert review_brief._OBSOLETE_RE.findall(text) == ["old_flag"]
