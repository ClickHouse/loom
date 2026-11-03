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


@pytest.mark.asyncio
async def test_render_run_blocks_keeps_a_narrowed_token_to_its_own_user() -> None:
    """A token bounded to one user that names another user's run_id reads none of
    that user's notes: the statement carries the request's read scope."""
    from server.identity.domain.read_scope import (
        WHOLE_NAMESPACE,
        ReadFilters,
        bind_request_read_filters,
    )
    from server.memory.app.working_memory import render_run_blocks

    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.calls.append((sql, params or {}))
            return []

    ch = _CH()
    bind_request_read_filters(None, ReadFilters("own_session", "u1", "s1"))
    try:
        await render_run_blocks("o", "ns", "run-of-u2", ch=ch)
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    sql, params = ch.calls[0]
    assert "user_id = {scope_uid:String}" in sql and params["scope_uid"] == "u1"
    assert "session_id = {scope_sid:String}" in sql and params["scope_sid"] == "s1"


def test_default_on() -> None:
    pass
