"""The identifier shapes read from a question, a code query or a stored excerpt match in
linear time. Each one used to repeat a group whose body could also continue the previous
repetition (`(?:[A-Z][a-zA-Z0-9]+)+`), so a run of capitals or digits that ended in a word
character backtracked through every way to split it: a 40-character token took seconds."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from server.code.domain.idents import embedded_idents
from server.llm import fake
from server.memory.app.ask.identifiers import _injectable_identifiers, _technical_identifiers
from server.memory.domain.decision_extract import _BARE_CAMEL
from server.tracker.domain.catalogue import extract_anchors

# Each split point doubled the old patterns' work; at 2,000 characters they never finish.
HOSTILE = ("Ab" + "A" * 2_000 + "_", "ab" + "A" * 2_000 + "_", "Ab" + "0" * 2_000 + "_")

CALLS: dict[str, Callable[[str], Any]] = {
    "memory.ask technical identifiers": _technical_identifiers,
    "memory.ask injectable identifiers": _injectable_identifiers,
    "code.search embedded identifiers": embedded_idents,
    "tracker.ask anchors": extract_anchors,
    "decision extract bare camel": _BARE_CAMEL.match,
    "fake model mentions": fake._CAMEL_RE.findall,
}


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
@pytest.mark.parametrize("text", HOSTILE, ids=["capitals", "camel", "digits"])
def test_a_long_identifier_run_is_matched_in_linear_time(
    call: Callable[[str], Any], text: str
) -> None:
    start = time.process_time()
    call(text)
    assert time.process_time() - start < 1.0


def test_the_shapes_still_match_what_they_named() -> None:
    assert "FooBar" in _technical_identifiers("why does FooBar fail")
    assert "MergeTreeData" in _injectable_identifiers("does MergeTreeData retry")
    assert embedded_idents("getUser and FooBar and foo_bar") == {"getuser", "foobar", "foo_bar"}
    assert extract_anchors("why does MergeTreeData crash").components == ["MergeTreeData"]
    assert _BARE_CAMEL.match("StorageMergeTree") and not _BARE_CAMEL.match("Storage")
    assert fake._CAMEL_RE.findall("call FooBar now") == ["FooBar"]
