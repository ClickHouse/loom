"""A grep excerpt shows the signature and docstring of the definition its hit sits in."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app.grep import _matching_lines
from server.code.app.grep_source import GREP_HEADER_PAGE_LINES, collect_contexts, page_contexts
from server.code.domain import deadline
from server.code.domain.definition_header import DEFINITION_HEADER_SCAN_LINES, DOCSTRING_MAX_LINES
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from tests.unit.clock_fake import ManualClock

_REVISION = "a" * 40
_ONCE = (
    "    def record_once(self, func):\n"
    '        """Works like record, but the function is only called once.\n'
    "        A second registration does not call it.\n"
    '        """\n'
    "\n"
    "        def wrapper(state):\n"
    "            if state.first_registration:\n"
    "                func(state)\n"
    "\n"
    "        return self.record(update_wrapper(wrapper, func))\n"
)


def _definition(path: str, scope: str, start: int, text: str) -> dict[str, object]:
    return {
        "path": path,
        "scope": scope,
        "kind": "function",
        "start_line": start,
        "end_line": start + text.count("\n") - 1,
        "content": text,
        "indexed_commit": _REVISION,
    }


def _excerpts(chunks: list[dict[str, object]], pattern: str, radius: int = 1, **options):
    spans = {
        (str(chunk["path"]), str(chunk["scope"])): (
            cast(int, chunk["start_line"]),
            cast(int, chunk["end_line"]),
        )
        for chunk in chunks
    }
    commits: dict[tuple[str, int], str] = {}
    request = CodeGrepInput(pattern=pattern, context_lines=radius, include_body=False)
    matches, _ = _matching_lines(chunks, spans, request, None, 50, match_commits=commits)
    pool = collect_contexts(
        chunks, matches, radius, 100_000, match_commits=commits, header_hits=matches
    )
    return matches, pool, page_contexts(pool, matches, radius, **options)


def test_grep_excerpt_shows_the_signature_and_docstring_its_hit_sits_under() -> None:
    path = "src/flask/blueprints.py"
    chunk = _definition(path, "flask.blueprints::Blueprint::record_once", 228, _ONCE)
    matches, pool, centred = _excerpts([chunk], "first_registration")
    lines = _ONCE.splitlines(keepends=True)

    assert [hit["line"] for hit in matches] == [234]
    hit_window = {"path": path, "start_line": 233, "end_line": 235, "text": "".join(lines[5:8])}
    assert centred == {"contexts": [hit_window], "contexts_partial": False}
    assert page_contexts(pool, matches, 1, headers=True) == {
        "contexts": [
            {"path": path, "start_line": 228, "end_line": 231, "text": "".join(lines[:4])},
            hit_window,
        ],
        "contexts_partial": False,
    }
    # a pool collected for match windows alone has no header line to show
    commits = {(path, 234): _REVISION}
    narrow = collect_contexts([chunk], matches, 1, 100_000, match_commits=commits)
    assert page_contexts(narrow, matches, 1, headers=True) == centred
    other = _definition("src/blueprints.cpp", "Blueprint::record_once", 228, _ONCE)
    _, _, unchanged = _excerpts([other], "first_registration", headers=True)
    assert [(row["start_line"], row["end_line"]) for row in unchanged["contexts"]] == [(233, 235)]


def test_grep_excerpt_marks_the_docstring_lines_it_leaves_out() -> None:
    def documented(lines: int) -> str:
        middle = "".join(f"    line {number}\n" for number in range(2, lines))
        return f'def target():\n    """Line 1\n{middle}    """\n    return found()\n'

    def windows(text: str, pattern: str = "found") -> list[tuple[int, int, object]]:
        _, _, page = _excerpts([_definition("m.py", "m::target", 1, text)], pattern, headers=True)
        assert not page["contexts_partial"]
        return [
            (row["start_line"], row["end_line"], row.get("docstring_elided"))
            for row in page["contexts"]
        ]

    # 12 docstring lines are shown whole; the hit window follows them
    assert windows(documented(12)) == [(1, 14, None)]
    # a gap of one or two lines costs more than the lines: they are shown
    assert windows(documented(14)) == [(1, 16, None)]
    assert windows(documented(15)) == [(1, 10, {"start_line": 11, "end_line": 13}), (14, 17, None)]
    # 20 lines keep the first 9 and the last 3, joined to the hit window
    assert windows(documented(20)) == [(1, 10, {"start_line": 11, "end_line": 18}), (19, 22, None)]
    # a hit inside the cut is shown; the gap of three lines is named, the one of two shown
    assert windows(documented(20), "line 14") == [
        (1, 10, {"start_line": 11, "end_line": 13}),
        (14, 21, None),
    ]
    assert windows(documented(24), "line 15") == [
        (1, 10, {"start_line": 11, "end_line": 14}),
        (15, 17, {"start_line": 18, "end_line": 22}),
        (23, 25, None),
    ]
    # the docstring ends past the lines read: the cut has no known end
    long = documented(DEFINITION_HEADER_SCAN_LINES + 20)
    assert windows(long) == [(1, 10, {"start_line": 11}), (101, 102, None)]
    # the lines read stop at the signature: no docstring is known to be left out
    hit = {"path": "m.py", "line": 1, "scope": "m::target"}
    signature = {"m.py": {"1": "def target():\n", "2": None}}
    span = {"scope_start_line": 1, "scope_end_line": 9}
    assert page_contexts(signature, [{**hit, **span}], 1, headers=True) == {
        "contexts": [{"path": "m.py", "start_line": 1, "end_line": 1, "text": "def target():\n"}],
        "contexts_partial": True,  # line 2 is in the match window
    }


async def test_a_hit_in_a_later_definition_of_the_same_name_keeps_its_match_window() -> None:
    from server.code.app import grep

    getter = '    def value(self):\n        """Read the value."""\n        return self._value\n'
    between = "\n    @value.setter\n"
    setter = (
        "    def value(self, new):\n"
        '        """Store the value."""\n'
        "        self._value = new\n"
        "        self.changed()\n"
    )
    scope = "box::Box::value"
    chunks = [_definition("box.py", scope, 3, getter), _definition("box.py", scope, 8, setter)]
    # no symbol row resolves the pair: the two definitions share one span
    spans = await grep._unit_spans(cast(CodeRepo, object()), "org", "ns", chunks)
    assert spans == {("box.py", scope): (3, 11)}
    query = CodeGrepInput(pattern="changed()", context_lines=1, include_body=False)
    commits: dict[tuple[str, int], str] = {}
    matches, _ = _matching_lines(chunks, spans, query, None, 50, match_commits=commits)
    body = "".join(setter.splitlines(keepends=True)[2:])
    hit_window = {"path": "box.py", "start_line": 10, "end_line": 11, "text": body}

    # the lines between the two are not held: the getter's header is not the setter's
    pool = collect_contexts(chunks, matches, 1, 100_000, match_commits=commits, header_hits=matches)
    assert page_contexts(pool, matches, 1, headers=True)["contexts"] == [hit_window]
    # they are held: a line at the getter's indentation ends it before the hit
    chunks.insert(1, {**_definition("box.py", "", 6, between), "kind": "other"})
    pool = collect_contexts(chunks, matches, 1, 100_000, match_commits=commits, header_hits=matches)
    assert pool["box.py"]["7"] == "    @value.setter\n"
    assert page_contexts(pool, matches, 1, headers=True)["contexts"] == [hit_window]
    # a hit in the getter shows the getter's header
    first = [{**matches[0], "line": 5, "text": "return self._value"}]
    pool = collect_contexts(
        chunks, first, 1, 100_000, match_commits={("box.py", 5): _REVISION}, header_hits=first
    )
    assert [
        (row["start_line"], row["end_line"])
        for row in page_contexts(pool, first, 1, headers=True)["contexts"]
    ] == [(3, 6)]


def test_grep_headers_stay_within_the_page_line_budget() -> None:
    documentation = "".join(f"    line {number}\n" for number in range(2, 12))
    body = "    first()\n    return found()\n    # end\n"
    large = f'def large():\n    """Line 1\n{documentation}    """\n{body}'
    small = f'def small():\n    """Short."""\n{body}'
    size = large.count("\n")
    chunks = [
        _definition("m.py", f"m::large{index}", 1 + index * size, large) for index in range(5)
    ]
    chunks.append(_definition("m.py", "m::small", 1 + 5 * size, small))
    _, _, page = _excerpts(chunks, "found", headers=True)

    shown = [
        any(row["start_line"] <= chunk["start_line"] <= row["end_line"] for row in page["contexts"])
        for chunk in chunks
    ]
    # each large header adds 13 lines: three fit, and the small one after them still does
    assert GREP_HEADER_PAGE_LINES == 48
    assert shown == [True, True, True, False, False, True]
    assert not any("docstring_elided" in row for row in page["contexts"])
    _, _, centred = _excerpts(chunks, "found")
    added = sum(row["text"].count("\n") for row in page["contexts"]) - sum(
        row["text"].count("\n") for row in centred["contexts"]
    )
    assert added == 3 * 13 + 2 <= GREP_HEADER_PAGE_LINES


@pytest.mark.parametrize("include_body", [False, True])
async def test_grep_reads_a_header_outside_the_scanned_chunks_from_verified_source(
    monkeypatch: pytest.MonkeyPatch, include_body: bool
) -> None:
    from server.code.app import grep, grep_source

    path = "src/worker.py"
    source = 'def target():\n    """Run once."""\n    first()\n    second()\n    return hit()\n'
    # one window of a definition indexed in parts
    chunk = {
        "path": path,
        "scope": "worker::target",
        "start_line": 4,
        "end_line": 5,
        "content": "    second()\n    return hit()\n",
        "indexed_commit": _REVISION,
    }
    match = {
        "path": path,
        "line": 5,
        "text": "return hit()",
        "scope": "worker::target",
        "scope_start_line": 1,
        "scope_end_line": 5,
    }
    asked: list[tuple[int, int]] = []

    async def fill(repo, org, ns, results, *, expected_revisions, max_source_bytes):
        from server.code.api.models_read import CodeReadWindow

        assert expected_revisions == {path: _REVISION}
        lines = source.splitlines(keepends=True)
        for result in results:
            asked.append((result.start_line, result.end_line))
            if result.start_line > len(lines):
                result.status = "out_of_range"
                continue
            text = "".join(lines[result.start_line - 1 : result.end_line])
            result.windows = [
                CodeReadWindow(start_line=result.start_line, end_line=result.end_line, text=text)
            ]

    monkeypatch.setattr(grep_source, "fill_verified_ranges", fill)
    scan = grep._GrepLineScan([chunk], [match], {(path, 5): _REVISION}, 1, False, "", "", "")
    reply, _, _, _ = await grep._grep_page_context(
        cast(CodeRepo, object()),
        "org",
        "ns",
        CodeGrepInput(pattern="hit()", context_lines=1, include_body=include_body),
        scan,
        [match],
        None,
    )

    hit_window = {"path": path, "start_line": 4, "end_line": 5, "text": chunk["content"]}
    header = {"path": path, "start_line": 1, "end_line": 2, "text": source[: source.index("    f")]}
    assert reply == {
        "contexts": [hit_window] if include_body else [header, hit_window],
        "contexts_partial": False,
    }
    # the match window's gap is read first
    assert asked == ([(6, 6)] if include_body else [(6, 6), (1, 3)])


async def test_a_later_grep_page_shows_its_own_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app import grep, grep_source, search_cursor

    path = "src/worker.py"
    text = 'def first():\n    """One."""\n    setup()\n    return found()\n'
    chunks = [
        _definition(path, "worker::first", 1, text),
        _definition(path, "worker::second", 5, text.replace("first", "second")),
    ]
    spans = {(path, "worker::first"): (1, 4), (path, "worker::second"): (5, 8)}
    query = CodeGrepInput(pattern="found", context_lines=1, include_body=False, limit=1)
    commits: dict[tuple[str, int], str] = {}
    hits, _ = _matching_lines(chunks, spans, query, None, 50, match_commits=commits)
    scan = grep._GrepLineScan(chunks, hits, commits, 2, False, "", "", "")
    repo = cast(CodeRepo, object())
    recovered: list[tuple[int, int]] = []

    async def fill(repo, org, ns, results, *, expected_revisions, max_source_bytes):
        from server.code.api.models_read import CodeReadWindow

        lines = (text + text.replace("first", "second")).splitlines(keepends=True)
        for result in results:
            recovered.append((result.start_line, result.end_line))
            if result.start_line > len(lines):
                result.status = "out_of_range"
                continue
            body = "".join(lines[result.start_line - 1 : result.end_line])
            result.windows = [
                CodeReadWindow(start_line=result.start_line, end_line=result.end_line, text=body)
            ]

    monkeypatch.setattr(grep_source, "fill_verified_ranges", fill)
    first, source, _, _ = await grep._grep_page_context(
        repo, "org", "ns", query, scan, hits[:1], None
    )
    assert [(row["start_line"], row["end_line"]) for row in first["contexts"]] == [(1, 5)]
    assert not recovered  # the first page's header came with its scanned chunk
    # the pool keeps match windows alone: the first page's header lines are not in it
    assert sorted(source[path], key=int) == ["3", "4", "5", "7", "8"]

    stored = {}

    async def pool(org, ns, key, fields):
        return {"payload": stored[key]}

    async def empty(*_, **__):
        return {}

    monkeypatch.setattr(search_cursor, "enabled", lambda: True)
    monkeypatch.setattr(
        search_cursor, "put_pool", lambda org, ns, key, payload, **_: stored.update({key: payload})
    )
    monkeypatch.setattr(grep, "_read_pool_settling", pool)
    monkeypatch.setattr(grep, "definitions_for", empty)
    monkeypatch.setattr(grep, "index_as_of", empty)
    monkeypatch.setattr(grep, "schedule_code_trace", lambda **_: None)
    scope = SearchScope()
    cursor = grep._issue_grep_cursor(
        query,
        "org",
        "ns",
        hits,
        served=1,
        run_id="pool",
        chunks_matched=2,
        truncated=False,
        context_source=source,
        match_commits=commits,
        scope=scope,
    )
    second, _, _ = await grep._serve_grep_page(
        query.model_copy(update={"cursor": cursor}), "org", "ns", repo, "grep", 0, scope=scope
    )

    assert second is not None and second.matches == hits[1:]
    # the second hit's header is read at the verified revision, after its window's gap
    assert recovered == [(9, 9), (6, 6)]
    assert [(row["start_line"], row["end_line"]) for row in second.contexts] == [(5, 8)]
    assert not second.contexts_partial


def _long_definitions(count: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Hits deep in `count` long functions, with only the lines around each hit indexed."""
    chunks, matches = [], []
    for index in range(count):
        path, scope = f"src/m{index}.py", f"m{index}::target"
        chunks.append(_definition(path, scope, 100, "    before()\n    found()\n    after()\n"))
        matches.append(
            {
                "path": path,
                "line": 101,
                "text": "found()",
                "scope": scope,
                "scope_start_line": 1,
                "scope_end_line": 102,
            }
        )
    return chunks, matches


async def _page(
    monkeypatch: pytest.MonkeyPatch, count: int, fill, budget: deadline.Budget | None = None
):
    from server.code.app import grep, grep_source

    monkeypatch.setattr(grep_source, "fill_verified_ranges", fill)
    chunks, matches = _long_definitions(count)
    commits = {(str(hit["path"]), 101): _REVISION for hit in matches}
    scan = grep._GrepLineScan(chunks, matches, commits, count, False, "", "", "")
    query = CodeGrepInput(pattern="found()", context_lines=1, include_body=False)
    page, pool, _, _ = await grep._grep_page_context(
        cast(CodeRepo, object()), "org", "ns", query, scan, matches, budget
    )
    return page, pool


async def test_a_page_reads_header_source_only_for_the_definitions_its_budget_can_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_read import CodeReadWindow

    header = 'def target():\n    """Run once."""\n'
    asked: list[tuple[str, int, int]] = []

    async def fill(repo, org, ns, results, *, expected_revisions, max_source_bytes):
        for result in results:
            asked.append((result.path, result.start_line, result.end_line))
            text = header + "    step()\n" * (result.end_line - 2)
            result.windows = [CodeReadWindow(start_line=1, end_line=result.end_line, text=text)]

    page, pool = await _page(monkeypatch, 10, fill)

    # an unread header is taken at the docstring cap: four fit the page's 48 lines
    assert GREP_HEADER_PAGE_LINES // DOCSTRING_MAX_LINES == 4
    assert asked == [(f"src/m{index}.py", 1, DEFINITION_HEADER_SCAN_LINES) for index in range(4)]
    assert [(row["path"], row["start_line"], row["end_line"]) for row in page["contexts"][:2]] == [
        ("src/m0.py", 1, 2),
        ("src/m0.py", 100, 102),
    ]
    assert sum(row["start_line"] == 1 for row in page["contexts"]) == 4
    assert not page["contexts_partial"]
    # the pool row keeps the match windows, as it does without headers
    assert all(sorted(lines, key=int) == ["100", "101", "102"] for lines in pool.values())


@pytest.mark.parametrize("elapsed_ms", [0, 200])
async def test_a_deadline_reads_no_file_for_a_header_alone(
    monkeypatch: pytest.MonkeyPatch, elapsed_ms: int
) -> None:
    asked: list[object] = []

    async def fill(repo, org, ns, results, *, expected_revisions, max_source_bytes):
        asked.extend(results)
        await asyncio.sleep(5)  # past any deadline left

    clock = ManualClock(t=100.0)
    monkeypatch.setattr(deadline, "time", SimpleNamespace(monotonic=clock.monotonic))
    budget = deadline.Budget(200, started=clock.t - elapsed_ms / 1000)
    page, _ = await _page(monkeypatch, 1, fill, budget)

    # the match windows are complete: the reply is whole, as it is without headers
    assert not asked
    assert not budget.partial
    assert [(row["start_line"], row["end_line"]) for row in page["contexts"]] == [(100, 102)]
    assert not page["contexts_partial"]


async def test_a_header_that_cannot_be_verified_is_not_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fill(repo, org, ns, results, *, expected_revisions, max_source_bytes):
        return None  # no readable mirror

    page, _ = await _page(monkeypatch, 1, fill)

    assert [(row["start_line"], row["end_line"]) for row in page["contexts"]] == [(100, 102)]
    assert not page["contexts_partial"]
