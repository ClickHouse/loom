"""The identifier shapes read from a question, a code query or a stored excerpt match a
run of capitals or digits ending in a word character in linear time. A repeated group whose
body can continue the previous repetition, `(?:[A-Z][a-zA-Z0-9]+)+`, backtracks through
every split of such a run instead: seconds at 25 to 40 characters, growing exponentially."""

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
CASES = [
    pytest.param(_technical_identifiers, "Ab" + "A" * 40 + "_", id="memory.ask technical"),
    pytest.param(_injectable_identifiers, "Ab" + "A" * 40 + "_", id="memory.ask injectable"),
    pytest.param(embedded_idents, "Ab" + "A" * 27 + "_", id="code.search PascalCase"),
    pytest.param(embedded_idents, "ab" + "A" * 27 + "_", id="code.search camelCase"),
    pytest.param(extract_anchors, "Ab" + "0" * 25 + "_", id="tracker.ask anchors"),
    pytest.param(_BARE_CAMEL.match, "Ab" + "A" * 27 + "_", id="decision extract bare camel"),
    pytest.param(fake._CAMEL_RE.findall, "Ab" + "A" * 27 + "_", id="fake model mentions"),
]


@pytest.mark.parametrize(("call", "text"), CASES)
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
    assert extract_anchors("why does S3Queue leak").components == ["S3Queue"]
    assert _BARE_CAMEL.match("StorageMergeTree") and not _BARE_CAMEL.match("Storage")
    assert fake._CAMEL_RE.findall("call FooBar now") == ["FooBar"]
