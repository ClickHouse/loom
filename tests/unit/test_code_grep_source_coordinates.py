"""Literal hits retain physical source coordinates through chunk presentation text."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app.grep import _line_coverage_fields, _matching_lines
from server.code.app.grep_source import (
    collect_contexts,
    match_revisions,
    page_contexts,
    verify_complete_contexts,
)
from server.code.app.symbol.uses import _uses_lines
from server.code.store.index import CodeRepo


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


@pytest.mark.parametrize(
    ("suffix_revision", "symbol_end", "symbol_revision"),
    [
        ("a" * 40, 9, "a" * 40),
        ("b" * 40, 9, "a" * 40),
        ("a" * 40, 13, "a" * 40),
        ("a" * 40, 9, "b" * 40),
    ],
)
async def test_grep_excerpt_uses_retained_lines_to_complete_a_short_definition(
    suffix_revision: str,
    symbol_end: int,
    symbol_revision: str,
) -> None:
    path, revision = "cli.py", "a" * 40
    preceding = "# Previous function\nbefore()\n\n"
    declaration = "def target():\n    first()\n    second()\n    third()\n"
    suffix = "    return done\n"
    chunks = [
        {
            "path": path,
            "start_line": 2,
            "end_line": 8,
            "content": preceding + declaration,
            "indexed_commit": revision,
            "scope": "target",
            "symbol_ids": ["target-id"],
        },
        {
            "path": path,
            "start_line": 9,
            "end_line": 9,
            "content": suffix,
            "indexed_commit": suffix_revision,
        },
    ]
    match = {"path": path, "line": 5, "scope": "target", "scope_start_line": 5, "scope_end_line": 9}
    commits = {(path, 5): revision}

    async def spans(org, ns, ids, *, source_only):
        assert (org, ns, ids, source_only) == ("org", "ns", ["target-id"], True)
        return [
            {
                "path": path,
                "qualified_name": "target",
                "start_line": 5,
                "end_line": symbol_end,
                "extent_commit": symbol_revision,
            }
        ]

    repo = cast(CodeRepo, SimpleNamespace(symbol_spans_by_ids=spans))
    proof = await verify_complete_contexts(repo, "org", "ns", chunks, [match], commits, 3, None)
    pool = collect_contexts(
        chunks, [match], 3, 1000, match_commits=commits, complete_contexts=proof
    )
    ordinary = {"path": path, "start_line": 2, "end_line": 8, "text": preceding + declaration}
    completed = {
        "path": path,
        "start_line": 3,
        "end_line": 9,
        "text": "before()\n\n" + declaration + suffix,
    }
    assert page_contexts(pool, [match], 3)["contexts"] == [ordinary]
    eligible = suffix_revision == symbol_revision == revision and symbol_end == 9
    assert page_contexts(pool, [match], 3, complete_contexts=proof) == {
        "contexts": [completed if eligible else ordinary],
        "contexts_partial": False,
    }


@pytest.mark.parametrize("verified", [True, False])
async def test_requested_grep_context_crosses_declaration_boundaries(
    monkeypatch: pytest.MonkeyPatch, verified: bool
) -> None:
    from server.code.app import grep, read

    revision = "a" * 40
    path = "tests/test_blueprints.py"
    source = "# previous test\n\ndef test_blueprint():\n    setup()\n    check()\n"
    chunk = {
        "path": path,
        "start_line": 3,
        "end_line": 4,
        "content": "def test_blueprint():\n    setup()\n",
        "indexed_commit": revision,
    }
    match = {"path": path, "line": 3}
    scan = grep._GrepLineScan([chunk], [match], {(path, 3): revision}, 1, False, "", "", "")
    calls = []

    async def paths(org, ns, wanted):
        assert (org, ns, wanted) == ("org", "ns", [path])
        return {
            path: {
                "indexed_commit": revision if verified else "b" * 40,
                "blob_sha": "blob",
                "chunk_count": 1,
                "lang": "python",
            }
        }

    async def present(_):
        return "verified-mirror"

    async def blob(git_dir, blob_path, commit, digest, *, max_bytes):
        calls.append((git_dir, blob_path, commit, digest, max_bytes))
        raw = source.encode()
        return raw, len(raw)

    monkeypatch.setattr(read, "repo_for_namespace", lambda *_: SimpleNamespace(path_prefixes=()))
    monkeypatch.setattr(read, "present_dir", present)
    monkeypatch.setattr(read, "_verified_blob", blob)
    reply, _, _, _ = await grep._grep_page_context(
        cast(CodeRepo, SimpleNamespace(file_rows_for_paths=paths)),
        "org",
        "ns",
        CodeGrepInput(pattern="def test_blueprint", context_lines=3, include_body=False),
        scan,
        [match],
        None,
    )

    assert len(calls) == int(verified)
    assert reply["contexts_partial"] is not verified
    window = reply["contexts"][0]
    assert window["start_line"] == (1 if verified else 3)
    assert window["end_line"] == (5 if verified else 4)
    assert window["text"] == (source if verified else chunk["content"])


async def test_cursor_does_not_recover_across_mixed_source_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import grep, grep_source, search_cursor

    path = "tests/test_blueprints.py"
    hits = [{"path": path, "line": 10}, {"path": path, "line": 12}]
    commits = {(path, 10): "b" * 40, (path, 12): "a" * 40}
    scan = grep._GrepLineScan([], hits, commits, 2, False, "", "", "")
    query = CodeGrepInput(pattern="target", context_lines=2, include_body=False, limit=1)
    repo = cast(CodeRepo, object())
    calls = []

    async def unexpected(*_, **__):
        calls.append("file read")

    monkeypatch.setattr(grep_source, "fill_verified_ranges", unexpected)
    first, source, _, _ = await grep._grep_page_context(
        repo, "org", "ns", query, scan, hits[:1], None
    )
    assert first == {"contexts": [], "contexts_partial": True}

    async def pool(*_):
        return {
            "payload": {
                "line_mapping": grep._GREP_LINE_MAPPING_VERSION,
                "matches": hits,
                "match_commits": list(commits.values()),
                "context_source": source,
            }
        }

    async def empty(*_, **__):
        return {}

    monkeypatch.setattr(search_cursor, "decode", lambda *_, **__: {"o": 1, "r": "pool"})
    monkeypatch.setattr(search_cursor, "expired", lambda _: False)
    monkeypatch.setattr(grep, "_read_pool_settling", pool)
    monkeypatch.setattr(grep, "definitions_for", empty)
    monkeypatch.setattr(grep, "index_as_of", empty)
    monkeypatch.setattr(grep, "schedule_code_trace", lambda **_: None)
    second, _, offset = await grep._serve_grep_page(
        query.model_copy(update={"cursor": "pool"}), "org", "ns", repo, "grep", 0
    )
    assert offset == 1
    assert second is not None and second.matches == hits[1:]
    assert second.contexts == [] and second.contexts_partial
    assert not calls


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


@pytest.mark.parametrize("alternate_revision", ["a" * 40, "b" * 40])
async def test_edit_context_keeps_candidates_when_grep_discards_a_conflicting_hit(
    alternate_revision, monkeypatch
) -> None:
    from unittest.mock import Mock

    from server.code.app.symbol import edit_target

    revision = "a" * 40
    chunks = [
        {
            "path": f"{name}.py",
            "scope": name,
            "start_line": 1,
            "end_line": 1,
            "content": f"def {name}():\n",
            "indexed_commit": revision,
        }
        for name in ("alpha", "beta")
    ]
    chunks.extend(
        [
            {
                "path": "gamma.py",
                "scope": "old",
                "start_line": 15,
                "end_line": 16,
                "content": "def old():\n    marker = 3\n",
                "indexed_commit": revision,
            },
            {
                "path": "gamma.py",
                "scope": "gamma",
                "start_line": 16,
                "end_line": 16,
                "content": "def gamma():\n",
                "indexed_commit": alternate_revision,
            },
        ]
    )
    spans = {(row["path"], row["scope"]): (row["start_line"], row["end_line"]) for row in chunks}
    clues = ["def alpha(", "def beta(", "def gamma(", "marker = 3"]
    commits: dict[tuple[str, int], str] = {}
    hits, capped = _matching_lines(
        chunks, spans, CodeGrepInput(pattern=clues), None, 20, match_commits=commits
    )
    assert not capped and not _line_coverage_fields(chunks, None).get("partial")
    assert [(hit["path"], hit["line"]) for hit in hits] == [
        ("alpha.py", 1),
        ("beta.py", 1),
        ("gamma.py", 16),
    ]
    revisions = match_revisions(hits, commits)
    assert revisions == {"alpha.py": revision, "beta.py": revision}
    repo = Mock()
    monkeypatch.setattr(edit_target, "CodeRepo", repo)
    assert await edit_target._declaration_targets("o", "n", hits, revisions, clues) == []
    repo.assert_not_called()


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
