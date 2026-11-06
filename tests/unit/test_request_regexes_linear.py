"""The regexes the API runs on request text read a long run in linear time.

Each case searches the way its caller does, on a repeat of the unit that
made the pattern quadratic: from n to 4n a linear read takes about 4 times
as long and a quadratic one 16 times. The pattern is read from its module at
call time, so the case measures what the server runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from server.code.app.index import sql_names
from server.code.domain import facets
from server.code.domain.index import citations, query_trim
from server.identity.app import scim_groups
from server.memory.app import observe_replay, value_updates
from server.memory.app.ask import identifiers, prompts
from server.memory.domain import activity_facts, conflicts, files, forget_requests, query_planner
from server.storage import frontend_assets
from server.tracker.api.models import (
    MAX_FRAME_CHARS,
    TrackerDuplicateCheckInput,
    TrackerSimilarInput,
)
from server.tracker.domain import similar

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


# (pattern, call as its caller makes it, text before the repeat, repeated unit)
CASES: list[tuple[str, Callable[[str], object], str, str]] = [
    ("facets._FILE_PATH_RE", _finditer(facets, "_FILE_PATH_RE"), "", "@"),
    ("facets._BARE_FILE_RE", _finditer(facets, "_BARE_FILE_RE"), "", "@"),
    ("files._URL_RE", _sub(files, "_URL_RE"), "", "a"),
    ("citations._PATH_RE", _finditer(citations, "_PATH_RE"), "", "@"),
    ("citations._LOC_RE", _finditer(citations, "_LOC_RE"), "", "+"),
    ("citations._BARE_LOC_RE", _finditer(citations, "_BARE_LOC_RE"), "", "a+"),
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
]


def _fastest_seconds(read: Callable[[str], object], text: str) -> float:
    """The fastest of three reads, so one stall on a busy runner does not count."""
    times = []
    for _ in range(3):
        started = time.perf_counter()
        read(text)
        times.append(time.perf_counter() - started)
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


def test_an_asset_name_longer_than_a_file_name_is_not_hashed() -> None:
    stem = "a" * (255 - len("-0123abcd.js"))
    assert frontend_assets.is_hashed_asset(f"assets/{stem}-0123abcd.js")
    assert not frontend_assets.is_hashed_asset(f"assets/{stem}x-0123abcd.js")
    # the directory does not count toward the name
    assert frontend_assets.is_hashed_asset("x" * 1000 + "/index-0123abcd.js")


def test_a_forget_sentence_past_the_cap_is_not_read() -> None:
    def sentence(pad: int) -> str:
        return "forget that I prefer Neovim" + "x" * pad + ", I switched to VS Code"

    at_cap = sentence(forget_requests.MAX_FORGET_SENTENCE_CHARS - len(sentence(0)))
    over = at_cap + "x"
    assert forget_requests.forget_sentences([{"role": "user", "content": at_cap}]) == {
        0: "I switched to VS Code"
    }
    assert forget_requests.forget_sentences([{"role": "user", "content": over}]) == {}


@pytest.mark.parametrize("field", ["frames", "symbols"])
def test_a_frame_line_is_bounded(field: str) -> None:
    model = TrackerSimilarInput if field == "frames" else TrackerDuplicateCheckInput
    model(namespace="t", **{field: ["a" * MAX_FRAME_CHARS]})
    with pytest.raises(ValidationError):
        model(namespace="t", **{field: ["a" * (MAX_FRAME_CHARS + 1)]})
    if field == "frames":
        TrackerDuplicateCheckInput(namespace="t", frames=["a" * MAX_FRAME_CHARS])
