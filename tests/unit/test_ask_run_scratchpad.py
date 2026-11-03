"""The run's scratchpad reaches the reader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.test_memory_ask import _FakeLLM, _hit, _search_output


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.memory.app.working_memory as wm
    import server.observability.budget as budget

    e = SimpleNamespace(
        llm=_FakeLLM("Blue [m1]."),
        scratch_calls=[],
        scratch="## scratchpad\nGoal: fix CI. Done: pinned runner image.",
    )

    async def fake_search(search_input: Any):
        return _search_output([_hit("m1", excerpt="favorite color: blue")])

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    async def fake_blocks(org: str, ns: str, run_id: str, **kw: Any) -> str:
        e.scratch_calls.append(run_id)
        return e.scratch

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(wm, "render_run_blocks", fake_blocks)
    return e


async def _run(**kw: Any):
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask.handler import handle_memory_ask

    return await handle_memory_ask(
        MemoryAskInput(org="o", namespace="ns", agent="*", query="what is my favorite color", **kw)
    )


@pytest.mark.asyncio
async def test_run_notes_precede_the_memories_when_a_run_id_is_present(env) -> None:
    out = await _run(run_id="run-1")
    assert out.source == "synthesized"
    assert env.scratch_calls == ["run-1"]
    system, user = env.llm.calls[-1]
    assert "Run notes (the agent's own scratchpad" in user
    assert "pinned runner image" in user and user.index("pinned runner image") < user.index("[m1]")
    assert "run notes precede the memories" in system


@pytest.mark.asyncio
async def test_no_run_id_reads_nothing(env) -> None:
    await _run()
    assert env.scratch_calls == []
    assert "Run notes" not in env.llm.calls[-1][1]


@pytest.mark.asyncio
async def test_empty_scratchpad_changes_nothing(env) -> None:
    env.scratch = ""
    await _run(run_id="run-3")
    assert "Run notes" not in env.llm.calls[-1][1]


@pytest.mark.asyncio
async def test_render_run_blocks_scopes_by_run_and_formats_newest_first() -> None:
    from server.memory.app.working_memory import render_run_blocks

    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.calls.append((sql, params or {}))
            return [
                {"block_key": "scratchpad", "content": "Goal: fix CI"},
                {"block_key": "plan", "content": " step 2 "},
                {"block_key": "empty", "content": "  "},
            ]

    ch = _CH()
    text = await render_run_blocks("o", "ns", "run-1", ch=ch)
    assert text == "## scratchpad\nGoal: fix CI\n\n## plan\nstep 2"
    sql, params = ch.calls[0]
    assert "run_id = {run:String}" in sql and "agent" not in sql and params["run"] == "run-1"
    assert await render_run_blocks("o", "ns", "-", ch=ch) == "" and len(ch.calls) == 1
    assert (await render_run_blocks("o", "ns", "run-1", ch=ch, max_chars=8)) == "## scrat"


class _RunCH:
    """working_memory rows of one run written by two users; a read returns the rows its
    user and session predicates admit."""

    rows = (
        {"block_key": "u1-plan", "content": "u1 note", "user_id": "u1", "session_id": "s1"},
        {"block_key": "u2-plan", "content": "u2 note", "user_id": "u2", "session_id": "s2"},
    )

    def __init__(self) -> None:
        self.calls = 0

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls += 1
        p = params or {}
        return [
            r
            for r in self.rows
            if ("{scope_uid:String}" not in sql or r["user_id"] == p["scope_uid"])
            and ("{scope_sid:String}" not in sql or r["session_id"] == p["scope_sid"])
        ]


@pytest.mark.asyncio
async def test_run_notes_stay_inside_a_narrowed_read_scope() -> None:
    """A token bounded to one user that names a run another user also wrote to reads
    only its own notes, on memory.ask and on memory.search alike."""
    from server.identity.domain.read_scope import ReadFilters, read_filters_bound
    from server.memory.app import scratchpad
    from server.memory.app.working_memory import render_run_blocks

    with read_filters_bound(None, ReadFilters("own_session", "u1", "s1")):
        assert await render_run_blocks("o", "ns", "run-1", ch=_RunCH()) == "## u1-plan\nu1 note"
        out = await scratchpad.load(
            "o", "ns", "*", "u1", "s1", "run-1", max_chars=2000, ch=_RunCH()
        )
    assert out is not None and out.block_keys == ["u1-plan"]
    assert "u2 note" in await render_run_blocks("o", "ns", "run-1", ch=_RunCH())


@pytest.mark.asyncio
async def test_a_refused_read_scope_reads_no_run_notes() -> None:
    from server.core.errors import IdentityIncompleteError
    from server.identity.domain.read_scope import (
        bind_request_read_filters,
        clear_request_read_filters,
    )
    from server.memory.app import scratchpad
    from server.memory.app.working_memory import render_run_blocks

    ch = _RunCH()
    bind_request_read_filters(None, IdentityIncompleteError("no user"))
    try:
        assert await render_run_blocks("o", "ns", "run-1", ch=ch) == ""
        # memory.search's reader raises before its statement; the search drops the notes.
        with pytest.raises(IdentityIncompleteError):
            await scratchpad.load("o", "ns", "*", None, None, "run-1", max_chars=2000, ch=ch)
    finally:
        clear_request_read_filters()
    assert ch.calls == 0


def test_default_on() -> None:
    pass
