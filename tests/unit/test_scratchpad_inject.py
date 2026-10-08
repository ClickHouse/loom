"""The run's scratchpad on memory.search and context.pack (memory.ask is
covered elsewhere): `scratchpad.load` reads the run's blocks by run_id
then widens to the session's run-less blocks and truncates with a marker;
memory.search carries the blocks only when a session or run was named and
the flag is on; context.pack for a run also renders the session-scoped
blocks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.naming import SENTINEL
from server.memory.app import scratchpad as sp

# ------------------------------------------------------------------ render


def test_render_joins_blocks_and_truncates_with_marker() -> None:
    out = sp.render(
        [
            {"block_key": "scratchpad", "content": "- 10:00Z decided on RRF"},
            {"block_key": "session:loom", "content": "goal: ship 0442"},
            {"block_key": "empty", "content": "   "},
        ],
        max_chars=0,
    )
    assert out.block_count == 2
    assert out.block_keys == ["scratchpad", "session:loom"]
    assert (
        out.rendered == "## scratchpad\n- 10:00Z decided on RRF\n\n## session:loom\ngoal: ship 0442"
    )
    assert not out.truncated

    cut = sp.render([{"block_key": "k", "content": "x" * 500}], max_chars=60)
    assert cut.truncated
    assert cut.rendered.endswith(sp.TRUNCATED_MARK)
    assert len(cut.rendered) <= 60
    assert cut.total_chars == 500  # what was stored, not what was shown


def test_render_empty_when_nothing_has_content() -> None:
    assert sp.render([{"block_key": "k", "content": ""}], max_chars=100).block_count == 0


# -------------------------------------------------------------------- load


class _CH:
    """Rows keyed by run_id; records the predicates each query used."""

    def __init__(self, by_run: dict[str, list[dict[str, Any]]]) -> None:
        self.by_run = by_run
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        self.queries.append((sql, params))
        run = params["run"] if "run_id = {run:String}" in sql else params.get("norun", SENTINEL)
        return list(self.by_run.get(run, []))


def _load(ch: _CH, **kw: Any) -> sp.ScratchpadBlocks | None:
    args = dict(
        org="o",
        namespace="ns",
        agent="*",
        user_id=None,
        session_id=None,
        run_id=None,
        max_chars=2000,
        ch=ch,
    )
    args.update(kw)
    return asyncio.run(sp.load(**args))


def test_load_needs_a_session_or_a_run() -> None:
    ch = _CH({SENTINEL: [{"block_key": "k", "content": "v"}]})
    assert _load(ch) is None
    assert _load(ch, session_id=SENTINEL, run_id=SENTINEL) is None
    assert ch.queries == []


def test_load_run_blocks_first_then_session_blocks() -> None:
    ch = _CH(
        {
            "cc-1": [{"block_key": "scratchpad", "content": "- 10:00Z picked RRF"}],
            SENTINEL: [
                {"block_key": "session:loom", "content": "goal: ship"},
                {"block_key": "scratchpad", "content": "stale copy at session scope"},
            ],
        }
    )
    out = _load(ch, session_id="s1", run_id="cc-1")
    assert out is not None
    assert out.scope == "run"
    assert out.block_keys == ["scratchpad", "session:loom"]
    assert "stale copy" not in out.rendered  # run-scoped block wins its key
    assert len(ch.queries) == 2


def test_load_widens_to_session_when_the_run_has_nothing() -> None:
    ch = _CH({SENTINEL: [{"block_key": "session:loom", "content": "goal: ship"}]})
    out = _load(ch, session_id="s1", run_id="cc-9")
    assert out is not None and out.scope == "session"
    assert out.rendered == "## session:loom\ngoal: ship"


def test_load_run_only_reads_agent_scope_rows() -> None:
    """The plugin writes at run scope with session_id '-'; a request that
    names only the run must bind the session axis to '-' to find them, and
    must widen to the plain agent-scope note too."""
    ch = _CH(
        {
            "cc-1": [{"block_key": "scratchpad", "content": "- 10:00Z x"}],
            SENTINEL: [{"block_key": "session:loom", "content": "goal"}],
        }
    )
    out = _load(ch, run_id="cc-1")
    assert out is not None and out.block_keys == ["scratchpad", "session:loom"]
    run_sql, _ = ch.queries[0]
    assert "session_id" not in run_sql and "agent" not in run_sql  # run id is the scope
    _, wide_params = ch.queries[1]
    assert wide_params["sid"] == SENTINEL


def test_load_open_axes_are_not_filtered() -> None:
    ch = _CH({SENTINEL: [{"block_key": "k", "content": "v"}]})
    _load(ch, session_id="s1", agent="*", user_id=SENTINEL)
    sql, params = ch.queries[0]
    assert "agent =" not in sql and "user_id =" not in sql
    _load(ch, session_id="s1", agent="claude-code", user_id="u7")
    sql, params = ch.queries[1]
    assert "agent = {agent:String}" in sql and params["agent"] == "claude-code"
    assert "user_id = {uid:String}" in sql and params["uid"] == "u7"


def test_load_none_when_nothing_stored() -> None:
    assert _load(_CH({}), session_id="s1", run_id="cc-1") is None


# ------------------------------------------------------- memory.search field


def _patch_load(monkeypatch: pytest.MonkeyPatch, blocks: sp.ScratchpadBlocks | None) -> list[Any]:
    calls: list[Any] = []

    async def fake_load(*a: Any, **k: Any) -> sp.ScratchpadBlocks | None:
        calls.append((a, k))
        return blocks

    monkeypatch.setattr(sp, "load", fake_load)
    return calls


def test_search_attaches_blocks_only_for_a_scoped_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.search.output import _attach_scratchpad
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_scratchpad_max_chars", 777)
    blocks = sp.render([{"block_key": "scratchpad", "content": "note"}], max_chars=0)
    calls = _patch_load(monkeypatch, blocks)

    out = SimpleNamespace(scratchpad="", scratchpad_blocks=0)
    inp = SimpleNamespace(
        org="o", namespace="ns", agent="*", user_id=None, session_id=None, run_id=None
    )
    asyncio.run(_attach_scratchpad(inp, out))
    assert calls == [] and out.scratchpad == ""

    inp.run_id = "cc-1"
    asyncio.run(_attach_scratchpad(inp, out))
    assert len(calls) == 1 and calls[0][1]["max_chars"] == 777
    assert out.scratchpad == "## scratchpad\nnote" and out.scratchpad_blocks == 1


def test_search_read_error_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.search.output import _attach_scratchpad

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("ch down")

    monkeypatch.setattr(sp, "load", boom)
    out = SimpleNamespace(scratchpad="", scratchpad_blocks=0)
    asyncio.run(
        _attach_scratchpad(
            SimpleNamespace(org="o", namespace="ns", session_id="s", run_id=None), out
        )
    )
    assert out.scratchpad == "" and out.scratchpad_blocks == 0


def test_search_output_model_has_the_fields() -> None:
    from server.memory.api.models_search import MemorySearchOutput

    assert MemorySearchOutput.model_fields["scratchpad"].default == ""
    assert MemorySearchOutput.model_fields["scratchpad_blocks"].default == 0


# ----------------------------------------------------------- context.pack


def test_context_pack_for_a_run_also_renders_the_session_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from html import escape

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    seen_runs: list[str] = []

    async def fake_render(inp: Any) -> Any:
        seen_runs.append(inp.run_id)
        if inp.run_id == "cc-1":
            blocks = [SimpleNamespace(block_key="scratchpad", content="- 10:00Z picked RRF")]
        else:
            blocks = [
                SimpleNamespace(block_key="session:loom", content="goal: ship 0442"),
                SimpleNamespace(block_key="scratchpad", content="older copy"),
            ]
        rendered = "\n".join(
            f'<working-memory block="{escape(b.block_key, quote=True)}">{escape(b.content)}</working-memory>'
            for b in blocks
        )
        return SimpleNamespace(rendered=rendered, blocks=blocks, block_count=len(blocks))

    async def _empty(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(rendered="", blocks=[], block_count=0, matches=[], results=[])

    monkeypatch.setattr(
        "server.memory.app.working_memory.handle_working_memory_render", fake_render
    )
    monkeypatch.setattr("server.memory.app.stack.handle_memory_stack", _empty)
    monkeypatch.setattr("server.memory.app.profile.handle_profile_render", _empty)
    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _empty)

    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="claude-code",
                run_id="cc-1",
                only_sections=["working_memory"],
            )
        )
    )
    assert seen_runs == ["cc-1", SENTINEL]
    wm = out.sections["working_memory"]
    assert wm.index('block="scratchpad">- 10:00Z picked RRF') < wm.index('block="session:loom"')
    assert "older copy" not in wm
    assert out.section_meta["working_memory"]["source_count"] == 2
    assert out.section_meta["working_memory"]["citations"] == [
        "working_memory:scratchpad",
        "working_memory:session:loom",
    ]


def test_context_pack_without_a_run_renders_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    seen_runs: list[str] = []

    async def fake_render(inp: Any) -> Any:
        seen_runs.append(inp.run_id)
        return SimpleNamespace(rendered="", blocks=[], block_count=0)

    async def _empty(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(rendered="", blocks=[], block_count=0, matches=[], results=[])

    monkeypatch.setattr(
        "server.memory.app.working_memory.handle_working_memory_render", fake_render
    )
    monkeypatch.setattr("server.memory.app.stack.handle_memory_stack", _empty)
    monkeypatch.setattr("server.memory.app.profile.handle_profile_render", _empty)
    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _empty)
    asyncio.run(
        handle_context_pack(
            ContextPackInput(org="o", namespace="ns", agent="a", only_sections=["working_memory"])
        )
    )
    assert seen_runs == [SENTINEL]
