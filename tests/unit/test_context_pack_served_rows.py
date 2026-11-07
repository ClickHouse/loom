"""context.pack records as served only the stored rows its rendered answer still shows:
a section cut to its budget, or the pack cut to its own, drops the rows past the cut."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.api.models_context import ContextPackInput
from server.memory.app.context_pack import handle_context_pack
from server.memory.domain.context_layout import served_rows


def test_served_rows_keeps_the_rows_whose_lines_survive_each_cut() -> None:
    rendered = (
        "note\n\n<memory_stack>\n- [pinned/a] one\n  continued\n- [profile/x] two\n"
        "- [pinned/b] thr...[truncated]\n</memory_stack>\n\n<memories>\n- [memory:n/c] four"
    )
    rows = {
        "memory_stack": [("n", "a"), None, ("n", "b"), ("n", "cut")],
        "memories": [("n", "c"), ("n", "d")],
        "procedures": [("n", "p")],
    }
    # the stack's fourth line was trimmed; memories' second, and all of procedures, by the pack
    shown = served_rows(rendered, ["memory_stack", "memories", "procedures"], rows)
    assert shown == [("n", "a"), ("n", "b"), ("n", "c")]


def _stub_all_but_the_stack(monkeypatch: pytest.MonkeyPatch, entries: list[Any]) -> None:
    async def _dead(*a: Any, **k: Any) -> Any:
        raise RuntimeError("not in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.procedure.handle_procedure_match",
        "server.memory.app.search.handle_memory_search",
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
        "server.knowledge.app.conversations.handle_conversation_search",
    ):
        monkeypatch.setattr(target, _dead)

    async def _stack(*a: Any, **k: Any) -> Any:
        return NS(entries=entries, tokens_used=0, tokens_budget=0, truncated=False)

    monkeypatch.setattr("server.memory.app.stack.handle_memory_stack", _stack)


def _entry(section: str, key: str, width: int) -> NS:
    return NS(section=section, memory_key=key, memory_type="fact", value="v" * width, actor="")


def test_a_stack_section_cut_to_its_budget_records_the_rows_it_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [_entry("profile", "role", 20)] + [_entry("pinned", f"p{i}", 150) for i in range(6)]
    _stub_all_but_the_stack(monkeypatch, entries)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="q",
                token_budget=4000,
                section_token_budgets={"memory_stack": 100},  # 400 chars
            )
        )
    )
    shown = out.sections["memory_stack"]
    assert out.section_meta["memory_stack"]["truncated"] is True
    kept = [k for _ns, k in out._served_rows]
    assert kept and kept == [f"p{i}" for i in range(len(kept))]
    assert len(kept) < 6
    # every recorded row's line is in what the agent got; the profile line is no row
    assert all(f"[pinned/{k} by=" in shown for k in kept)
    assert f"[pinned/p{len(kept)} by=" not in shown
