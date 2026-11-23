"""Grep bodies reuse structural reads and preserve matching-line evidence."""

from types import SimpleNamespace

import pytest

from server.code.api.models_grep import CodeGrepInput, CodeGrepOutput
from server.code.app.grep_bodies import definitions_for, select_definitions


@pytest.mark.asyncio
async def test_hits_share_one_definition_and_pages_keep_only_their_lines(monkeypatch):
    from server.code.app.symbol import enclosing

    async def read(input):
        assert input.include_body and input.token_budget == 0
        return SimpleNamespace(
            results=[
                {
                    "path": "a.py",
                    "line": line,
                    "symbol_id": "one",
                    "start_line": 1,
                    "end_line": 10,
                    "body": "complete body",
                    "body_elided": False,
                }
                for line in (3, 8)
            ]
        )

    monkeypatch.setattr(enclosing, "handle_code_enclosing", read)
    matches = [{"path": "a.py", "line": line} for line in (3, 8)]
    out = await definitions_for(CodeGrepInput(pattern="target", include_body=True), matches, None)
    assert out["definitions_partial"] is False
    assert len(out["definitions"]) == 1
    assert out["definitions"][0]["body"] == "complete body"
    assert out["definitions"][0]["lines"] == [3, 8]
    assert "line" not in out["definitions"][0]
    assert select_definitions(out["definitions"], [matches[1]])[0]["lines"] == [8]
    assert select_definitions(out["definitions"], [{"path": "other.py", "line": 3}]) == []


@pytest.mark.asyncio
async def test_default_is_unchanged_and_body_failures_are_disclosed(monkeypatch):
    from server.code.app.symbol import enclosing

    async def unavailable(_input):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(enclosing, "handle_code_enclosing", unavailable)
    matches = [{"path": "a.py", "line": 3}]
    assert await definitions_for(CodeGrepInput(pattern="target"), matches, None) == {}
    plain = CodeGrepOutput(matches=[], chunks_matched=0).model_dump()
    assert not {"definitions", "definitions_partial", "definitions_note"} & plain.keys()
    out = await definitions_for(CodeGrepInput(pattern="target", include_body=True), matches, None)
    assert out["definitions"] == [] and out["definitions_partial"] is True
    assert "could not be read" in out["definitions_note"]
    assert matches == [{"path": "a.py", "line": 3}]
    from server.observability.event_args import project_result

    assert project_result(out, op_name="code.grep")["out_definitions_partial"] == "true"


@pytest.mark.asyncio
async def test_all_requested_hits_are_read_beyond_one_structural_batch(monkeypatch):
    from server.code.app.symbol import enclosing

    calls = []

    async def read(input):
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
    matches = [{"path": "a.py", "line": line} for line in range(1, 121)]
    out = await definitions_for(CodeGrepInput(pattern="target", include_body=True), matches, None)
    assert list(map(len, calls)) == [50, 50, 20]
    assert len(out["definitions"]) == 120 and not out["definitions_partial"]
