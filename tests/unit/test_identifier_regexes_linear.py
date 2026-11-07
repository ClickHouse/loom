"""The identifier shapes read from a question, a code query or a stored excerpt match a
run of capitals or digits ending in a word character in linear time. A repeated group whose
body can continue the previous repetition, `(?:[A-Z][a-zA-Z0-9]+)+`, backtracks through
every split of such a run instead: seconds at 25 to 40 characters, doubling per character."""

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

# Each input takes a backtracking pattern 2 to 10 s, so a regression fails here, not hangs.
CASES: list[tuple[str, Callable[[str], Any], str]] = [
    ("memory.ask technical identifiers", _technical_identifiers, "Ab" + "A" * 40 + "_"),
    ("memory.ask injectable identifiers", _injectable_identifiers, "Ab" + "A" * 40 + "_"),
    ("code.search PascalCase", embedded_idents, "Ab" + "A" * 27 + "_"),
    ("code.search camelCase", embedded_idents, "ab" + "A" * 27 + "_"),
    ("tracker.ask anchors", extract_anchors, "Ab" + "0" * 25 + "_"),
    ("decision extract bare camel", _BARE_CAMEL.match, "Ab" + "A" * 27 + "_"),
    ("fake model mentions", fake._CAMEL_RE.findall, "Ab" + "A" * 27 + "_"),
]


@pytest.mark.parametrize(("call", "text"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
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
