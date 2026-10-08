"""Context.pack for a session also carries the agent's
working memory (session `-`, run `-`). The SessionStart primer names its new
session, so without this the note the Stop hook left (`session:<project>`,
agent scope) and any note an MCP client left for the next agent would never
reach it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.app.context_pack as ctx
import server.memory.app.working_memory as wm
from server.core.naming import SENTINEL


def _pack_input(**kw: Any) -> SimpleNamespace:
    base = dict(
        org="o",
        namespace="n",
        agent="claude-code",
        user_id="-",
        session_id=SENTINEL,
        run_id=SENTINEL,
        session_block="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def _store(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    rows = {
        ("s1", "cc-s1"): [("scratchpad", "run note")],
        ("s1", SENTINEL): [("scratchpad", "stale session copy"), ("plan", "session plan")],
        (SENTINEL, SENTINEL): [
            ("session:loom", "# Session state\ngoal: ship the fix\nfiles: a.py"),
            ("plan", "agent-wide plan (shadowed)"),
            ("loomcheck", "loomcheck complete"),
        ],
    }
    asked: list[tuple[str, str]] = []

    async def _render(inp: Any) -> Any:
        asked.append((inp.session_id, inp.run_id))
        blocks = [
            SimpleNamespace(block_key=k, content=c)
            for k, c in rows.get((inp.session_id, inp.run_id), [])
        ]
        return SimpleNamespace(blocks=blocks, rendered="")

    monkeypatch.setattr(wm, "handle_working_memory_render", _render)
    return asked


def test_a_session_pack_reaches_the_agents_blocks_narrowest_first(_store) -> None:
    blocks, rendered = asyncio.run(
        ctx._working_memory_layers(_pack_input(session_id="s1", run_id="cc-s1"))
    )
    assert _store == [("s1", "cc-s1"), ("s1", SENTINEL), (SENTINEL, SENTINEL)]
    assert [b.block_key for b in blocks] == ["scratchpad", "plan", "session:loom", "loomcheck"]
    assert "run note" in rendered and "stale session copy" not in rendered
    assert "session plan" in rendered and "shadowed" not in rendered
    # A multi-line block keeps its body, not only line one.
    assert "goal: ship the fix\nfiles: a.py</working-memory>" in rendered
    assert '<working-memory block="loomcheck">loomcheck complete</working-memory>' in rendered


def test_an_agent_scope_pack_reads_one_layer(_store) -> None:
    blocks, _ = asyncio.run(ctx._working_memory_layers(_pack_input()))
    assert _store == [(SENTINEL, SENTINEL)]
    assert [b.block_key for b in blocks] == ["session:loom", "plan", "loomcheck"]


def test_block_content_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _render(inp: Any) -> Any:
        return SimpleNamespace(
            blocks=[SimpleNamespace(block_key='a"b', content="<x> & y")], rendered=""
        )

    monkeypatch.setattr(wm, "handle_working_memory_render", _render)
    _, rendered = asyncio.run(ctx._working_memory_layers(_pack_input()))
    assert rendered == '<working-memory block="a&quot;b">&lt;x&gt; &amp; y</working-memory>'


def test_a_named_bookmark_keeps_other_projects_bookmarks_out(_store, monkeypatch) -> None:
    """The plugin names its own `session:<project>` block; the
    agent layer then leaves another project's bookmark out, keeps non-bookmark
    blocks, and without a name keeps today's behaviour."""
    rows = {
        (SENTINEL, SENTINEL): [
            ("session:loom", "loom bookmark"),
            ("session:example-private", "other repo bookmark"),
            ("loomcheck", "agent note"),
        ],
    }

    async def _render(inp: Any) -> Any:
        blocks = [
            SimpleNamespace(block_key=k, content=c)
            for k, c in rows.get((inp.session_id, inp.run_id), [])
        ]
        return SimpleNamespace(blocks=blocks, rendered="")

    monkeypatch.setattr(wm, "handle_working_memory_render", _render)
    named, _ = asyncio.run(ctx._working_memory_layers(_pack_input(session_block="session:loom")))
    assert [b.block_key for b in named] == ["session:loom", "loomcheck"]
    unnamed, _ = asyncio.run(ctx._working_memory_layers(_pack_input()))
    assert [b.block_key for b in unnamed] == [
        "session:loom",
        "session:example-private",
        "loomcheck",
    ]
