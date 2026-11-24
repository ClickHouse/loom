"""Literal hits retain physical source coordinates through chunk presentation text."""

from __future__ import annotations

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app.grep import _matching_lines
from server.code.app.grep_source import collect_contexts, page_contexts
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


def test_grep_context_unions_overlapping_windows_on_the_served_page() -> None:
    text = "def register():\n    after(target)\n    before(target)\n    return done\n"
    chunk = {
        "path": "cli.py",
        "scope": "register",
        "kind": "function",
        "start_line": 20,
        "end_line": 23,
        "content": "# Presentation prefix\n" + text,
        "indexed_commit": "a" * 40,
    }
    request = CodeGrepInput(pattern="target", context_lines=1)
    matches, _ = _matching_lines([chunk], {}, request, None, 20)
    pool = collect_contexts(
        [chunk],
        matches,
        request.context_lines,
        1000,
        match_commits={("cli.py", 21): "a" * 40, ("cli.py", 22): "a" * 40},
    )
    combined = page_contexts(pool, matches, request.context_lines)
    assert [hit["line"] for hit in matches] == [21, 22]
    assert combined["contexts"] == [
        {"path": "cli.py", "start_line": 20, "end_line": 23, "text": text}
    ]
    next_page = page_contexts(pool, matches[1:], request.context_lines)
    assert next_page["contexts"][0]["start_line"] == 21
    assert not combined["contexts_partial"]


def test_grep_context_discloses_conflicting_lines_from_duplicate_hits() -> None:
    row = {
        "path": "app.py",
        "scope": "",
        "start_line": 1,
        "end_line": 2,
        "content": "prepare()\ntarget()\n",
        "indexed_commit": "a" * 40,
    }
    chunks = [row, {**row, "content": "setup()\ntarget()\n"}]
    request = CodeGrepInput(pattern="target", context_lines=1)
    matches, _ = _matching_lines(chunks, {}, request, None, 20)
    assert [hit["line"] for hit in matches] == [2]
    pool = collect_contexts(
        chunks, matches, request.context_lines, 1000, match_commits={("app.py", 2): "a" * 40}
    )
    output = page_contexts(pool, matches, request.context_lines)
    assert output == {
        "contexts": [{"path": "app.py", "start_line": 2, "end_line": 2, "text": "target()\n"}],
        "contexts_partial": True,
    }


def test_grep_context_excludes_lines_from_other_revisions() -> None:
    row = {
        "path": "app.py",
        "scope": "",
        "start_line": 10,
        "end_line": 10,
        "content": "target()\n",
        "indexed_commit": "a" * 40,
    }
    other = {
        **row,
        "start_line": 11,
        "end_line": 11,
        "content": "# target at another revision\n",
        "indexed_commit": "b" * 40,
    }
    all_matches = [{"path": "app.py", "line": line} for line in (10, 11)]
    commits = {("app.py", 10): "a" * 40, ("app.py", 11): "b" * 40}
    matches = all_matches[:1]
    pool = collect_contexts([row, other], matches, 1, 1000, match_commits=commits)
    assert page_contexts(pool, matches, 1) == {
        "contexts": [{"path": "app.py", "start_line": 10, "end_line": 10, "text": "target()\n"}],
        "contexts_partial": True,
    }
    ambiguous = collect_contexts([row, other], all_matches, 1, 1000, match_commits=commits)
    assert page_contexts(ambiguous, all_matches, 1) == {"contexts": [], "contexts_partial": True}


def test_a_grep_line_uses_retained_chunk_provenance() -> None:
    row = {
        "path": "m.py",
        "scope": "",
        "start_line": 124,
        "end_line": 124,
        "content": "target = 1\n",
        "indexed_commit": "b" * 40,
    }
    request = CodeGrepInput(pattern="target")
    commits: dict[tuple[str, int], str] = {}
    matches, _ = _matching_lines([row], {}, request, None, 20, match_commits=commits)
    assert commits == {("m.py", 124): "b" * 40}
    assert "indexed_commit" not in matches[0]
    other = {**row, "indexed_commit": "a" * 40}
    repeated, _ = _matching_lines([row, other], {}, request, None, 20, match_commits=commits)
    assert repeated == matches
    assert commits == {("m.py", 124): ""}


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


def test_coverage_validation_does_not_parse_a_chunk_past_the_scan_bound(monkeypatch) -> None:
    from server.code.app import grep

    row = {
        "path": "large.py",
        "scope": "",
        "start_line": 1,
        "end_line": 1,
        "content": "target " * 100,
    }
    calls = []
    retained = grep.retained_source_lines

    def tracked(chunk):
        calls.append(chunk)
        return retained(chunk)

    monkeypatch.setattr(grep, "_LINE_SCAN_MAX_CHARS", 10)
    monkeypatch.setattr(grep, "retained_source_lines", tracked)
    matches, capped = grep._matching_lines([row], {}, CodeGrepInput(pattern="target"), None, 20)
    assert not matches and capped and not calls
    coverage = grep._line_coverage_fields([row], None)
    assert coverage["partial"] and "scan limit" in coverage["partial_reason"]
    assert not calls
