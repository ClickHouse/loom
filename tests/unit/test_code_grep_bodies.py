"""Grep bodies reuse structural reads and preserve matching-line evidence."""

from types import SimpleNamespace

import pytest

from server.code.api.models_grep import CodeGrepInput, CodeGrepOutput
from server.code.app.grep_bodies import definitions_for

COMMIT = "a" * 40


def _verified(matches):
    return {(m["path"], m["line"]): COMMIT for m in matches}


def test_current_extent_replaces_only_the_same_older_symbol():
    from server.code.app.symbol import enclosing

    stale = {
        "path": "a.cpp",
        "symbol_id": "run",
        "extent_commit": "b" * 40,
        "start_line": 1,
        "end_line": 1,
    }
    current = {**stale, "extent_commit": COMMIT, "end_line": 4}
    other = {**stale, "symbol_id": "other"}
    assert enclosing._by_path([stale, current, other], {"a.cpp": COMMIT}) == {
        "a.cpp": [current, other]
    }
    current_stub = {**current, "end_line": 1}
    older_complete = {**stale, "end_line": 4}
    assert enclosing._by_path([current_stub, older_complete], {"a.cpp": COMMIT}) == {
        "a.cpp": [older_complete]
    }


def test_guarded_symbol_resolution_keeps_shared_ids_in_distinct_files():
    from server.code.store.index.symbol_rank import _prefer_scip

    first = {
        "path": "a.cpp",
        "symbol_id": "run",
        "qualified_name": "run",
        "extent_commit": COMMIT,
        "tier": "scip",
    }
    second = {**first, "path": "b.cpp", "tier": "treesitter"}
    assert _prefer_scip([first, second]) == [first, second]


@pytest.mark.asyncio
async def test_enclosing_bodies_keep_shared_symbol_ids_separate_by_file(monkeypatch):
    from server.code.app.symbol import enclosing

    paths = {"a.cpp": "a" * 40, "b.cpp": "b" * 40}
    rows = [
        {
            "path": p,
            "symbol_id": "one",
            "qualified_name": "run",
            "signature": "(old)" if p == "b.cpp" else "()",
            "_extent_commit": "a" * 40,
        }
        for p in paths
    ]
    expected = {"a.cpp": "int run() { return 11; }", "b.cpp": "int run() { return 22; }"}

    async def read(_repo, _org, _ns, row, *, expected_commit):
        assert expected_commit == paths[row["path"]]
        return expected[row["path"]]

    monkeypatch.setattr(enclosing, "symbol_body", read)
    count, elided, _ = await enclosing._attach_enclosing_bodies(
        None, "org", "repo", rows, 0, set(), paths
    )
    assert (count, elided) == (2, 0)
    assert {r["path"]: r["body"] for r in rows} == expected
    assert [r["signature"] for r in rows] == ["()", ""]


def test_saved_match_rows_leave_revisions_for_the_page_reader(monkeypatch):
    from server.code.app import grep, search_cursor

    matches = [{"path": "a.py", "line": line, "text": "target()"} for line in (3, 8)]
    captured = []
    monkeypatch.setattr(search_cursor, "enabled", lambda: True)
    monkeypatch.setattr(search_cursor, "encode", lambda **_kwargs: "cursor")
    monkeypatch.setattr(
        search_cursor, "put_pool", lambda *args, **_kwargs: captured.append(args[3])
    )
    cursor = grep._issue_grep_cursor(
        CodeGrepInput(pattern="target"),
        "org",
        "repo",
        matches,
        served=1,
        run_id="run",
        chunks_matched=2,
        truncated=True,
        match_commits=_verified(matches),
    )
    assert cursor == "cursor"
    assert captured[0]["matches"] == matches
    assert captured[0]["match_commits"] == [COMMIT, COMMIT]


@pytest.mark.asyncio
async def test_hits_share_one_definition_and_pages_keep_only_their_lines(monkeypatch):
    from server.code.app.symbol import enclosing

    body = "target()\n" * 10

    async def read(input, *, body_revisions):
        assert input.include_body and input.token_budget == 0
        assert body_revisions == {"a.py": COMMIT}
        return SimpleNamespace(
            results=[
                {
                    "path": "a.py",
                    "line": line,
                    "symbol_id": "one",
                    "start_line": 1,
                    "end_line": 10,
                    "body": body,
                    "body_elided": False,
                }
                for line in (3, 8)
            ]
        )

    monkeypatch.setattr(enclosing, "handle_code_enclosing", read)
    matches = [{"path": "a.py", "line": line, "text": "target()"} for line in (3, 8)]
    out = await definitions_for(
        CodeGrepInput(pattern="target", include_body=True),
        matches,
        None,
        match_commits=_verified(matches),
    )
    assert out["definitions_partial"] is False
    assert len(out["definitions"]) == 1
    assert out["definitions"][0]["body"] == body
    assert out["definitions"][0]["lines"] == [3, 8]
    assert "line" not in out["definitions"][0]


@pytest.mark.asyncio
async def test_default_is_unchanged_and_body_failures_are_disclosed(monkeypatch):
    from server.code.app.symbol import enclosing

    async def unavailable(_input, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(enclosing, "handle_code_enclosing", unavailable)
    matches = [{"path": "a.py", "line": 3}]
    assert (
        await definitions_for(CodeGrepInput(pattern="target"), matches, None, match_commits={})
        == {}
    )
    plain = CodeGrepOutput(matches=[], chunks_matched=0).model_dump()
    assert not {"definitions", "definitions_partial", "definitions_note"} & plain.keys()
    out = await definitions_for(
        CodeGrepInput(pattern="target", include_body=True),
        matches,
        None,
        match_commits=_verified(matches),
    )
    assert out["definitions"] == [] and out["definitions_partial"] is True
    assert "could not be read" in out["definitions_note"]
    assert matches == [{"path": "a.py", "line": 3}]
    from server.observability.event_args import project_result

    assert project_result(out, op_name="code.grep")["out_definitions_partial"] == "true"


@pytest.mark.asyncio
async def test_all_requested_hits_are_read_beyond_one_structural_batch(monkeypatch):
    from server.code.app.symbol import enclosing

    calls = []

    async def read(input, **_kwargs):
        calls.append(input.locations)
        return SimpleNamespace(
            results=[
                {
                    "path": "a.py",
                    "line": int(loc.split(":")[1]),
                    "symbol_id": loc,
                    "start_line": int(loc.split(":")[1]),
                    "end_line": int(loc.split(":")[1]),
                    "body": loc,
                    "body_elided": False,
                }
                for loc in input.locations
            ]
        )

    monkeypatch.setattr(enclosing, "handle_code_enclosing", read)
    matches = [{"path": "a.py", "line": line, "text": f"a.py:{line}"} for line in range(1, 121)]
    out = await definitions_for(
        CodeGrepInput(pattern="target", include_body=True),
        matches,
        None,
        match_commits=_verified(matches),
    )
    assert list(map(len, calls)) == [50, 50, 20]
    assert len(out["definitions"]) == 120 and not out["definitions_partial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wider_body_available", [True, False])
async def test_decorator_and_body_hits_across_batches_share_the_wider_body(
    monkeypatch, wider_body_available
):
    from server.code.app.symbol import enclosing

    async def read(input, **_kwargs):
        results = []
        for location in input.locations:
            line = int(location.split(":")[1])
            body = "def run(): pass"
            if line == 50:
                body = "@command()\n" + body if wider_body_available else ""
            results.append(
                {
                    "path": "cli.py",
                    "line": line,
                    "symbol_id": "run" if line >= 50 else location,
                    "start_line": line,
                    "end_line": 51 if line >= 50 else line,
                    "body": body,
                }
            )
        return SimpleNamespace(results=results)

    monkeypatch.setattr(enclosing, "handle_code_enclosing", read)
    matches = [
        {"path": "cli.py", "line": line, "text": "@command()" if line == 50 else "def run(): pass"}
        for line in range(1, 52)
    ]
    out = await definitions_for(
        CodeGrepInput(pattern="run", include_body=True),
        matches,
        None,
        match_commits=_verified(matches),
    )
    assert out["definitions_partial"] is not wider_body_available
    assert len(out["definitions"]) == 50
    assert out["definitions"][-1]["lines"] == [50, 51]
    assert out["definitions"][-1]["body"].startswith("@command()") is wider_body_available


@pytest.mark.asyncio
async def test_stale_enclosing_rows_cannot_replace_the_match(monkeypatch):
    from server.code.app.symbol import enclosing

    hits = [{"path": "a.py", "line": 2, "scope": "a::run", "text": "target()"}]

    async def read(_input, **_kwargs):
        return SimpleNamespace(
            results=[
                {
                    "path": "a.py",
                    "line": 2,
                    "qualified_name": "a::run",
                    "start_line": 1,
                    "end_line": 2,
                    "body": "def run():\n    old_target()\n",
                },
            ]
        )

    monkeypatch.setattr(enclosing, "handle_code_enclosing", read)
    request = CodeGrepInput(pattern="target", include_body=True)
    for commits in ({}, _verified(hits)):
        out = await definitions_for(request, hits, None, match_commits=commits)
        assert out["definitions"] == []
        assert out["definitions_partial"] is True
