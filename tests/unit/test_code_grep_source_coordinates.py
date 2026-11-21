"""Literal hits retain physical source coordinates through chunk presentation text."""

from __future__ import annotations

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app.grep import _matching_lines
from server.code.app.symbol.uses import _uses_lines


@pytest.mark.parametrize(
    "prefix", ["Documents current_app.\n", "# … continued from def current_app():\n"]
)
def test_grep_and_mentions_ignore_non_source_prefixes(prefix: str) -> None:
    body = 'def response():\n    """Use current_app."""\n    return current_app.response_class()\n'
    row = {
        "path": "helpers.py",
        "scope": "response",
        "kind": "function",
        "start_line": 10,
        "end_line": 12,
        "content": prefix + body,
    }
    spans = {("helpers.py", "response"): (10, 12)}
    actual, capped = _matching_lines([row], spans, CodeGrepInput(pattern="current_app"), None, 20)
    assert not capped
    expected = [(11, '"""Use current_app."""'), (12, "return current_app.response_class()")]
    assert [(hit["line"], hit["text"]) for hit in actual] == expected
    assert [
        (hit["line"], hit["text"]) for hit in _uses_lines([row], ["current_app"], spans)
    ] == expected


def test_unicode_string_separators_are_not_source_line_boundaries() -> None:
    row = {
        "path": "m.py",
        "scope": "f",
        "kind": "function",
        "start_line": 1,
        "end_line": 3,
        "content": 'def f():\n    text = "before\u2028after"\n    return target()\n',
    }
    matches, _ = _matching_lines([row], {}, CodeGrepInput(pattern="target"), None, 20)
    assert [(hit["line"], hit["text"]) for hit in matches] == [(3, "return target()")]


def test_compacted_class_text_does_not_invent_a_matching_source_line() -> None:
    row = {
        "path": "m.py",
        "scope": "Container",
        "kind": "class_skeleton",
        "start_line": 1,
        "end_line": 100,
        "content": "class Container:\n    def first(self): ...\n    target = 1\n",
    }
    matches, _ = _matching_lines([row], {}, CodeGrepInput(pattern="target"), None, 20)
    assert matches == []


def test_rendered_message_grep_uses_the_same_source_coordinates() -> None:
    from server.code.app import message_grep

    row = {
        "path": "endpoint.cpp",
        "scope": "bind",
        "kind": "function",
        "start_line": 10,
        "end_line": 12,
        "content": 'Documents endpoint binding.\nvoid bind() {\n    throw Exception("Cannot bind public endpoint name");\n}\n',
    }
    ranked = message_grep.rank_statements(
        message_grep.split_message("Cannot bind public endpoint name"), [row]
    )
    assert ranked and ranked[0].line == 11
