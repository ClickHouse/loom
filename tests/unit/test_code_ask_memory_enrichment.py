"""code.ask reverse enrichment: fuse decision-memories into the code answer,
gated by an explicit memory-namespace binding (no-op when unset)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.code.app import answers as ca
from server.memory.api.models_search import MemorySearchInput


class _Cap:
    """An llm stub that captures the user prompt it was asked to synthesize."""

    def __init__(self) -> None:
        self.user = ""

    async def complete(self, system, user, max_tokens=0):
        self.user = user
        return SimpleNamespace(text="ok")


@pytest.mark.asyncio
async def test_no_op_when_memory_namespace_unbound(monkeypatch) -> None:
    monkeypatch.setattr(ca, "logger", ca.logger)  # touch, keep import stable
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "", raising=False)
    assert await ca._inject_memory_context("why is X like this?") == []


@pytest.mark.asyncio
async def test_returns_decision_memories_when_bound(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)

    async def fake_search(inp):
        # The enrichment imports its wire model and handler inside a fail-open
        # try: a broken import would answer [] here, not raise.
        assert isinstance(inp, MemorySearchInput)
        assert inp.namespace == "support-bot"
        return SimpleNamespace(
            abstain=False,
            results=[
                SimpleNamespace(
                    memory_key="user_decision_1",
                    content_excerpt="chose approach B because A double-freed",
                ),
                SimpleNamespace(
                    memory_key="user_pr_rationale",
                    content_excerpt="PR #123 reverted the guard per @maintainer",
                ),
            ],
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", fake_search)

    items = await ca._inject_memory_context("why does the guard exist?")
    assert len(items) == 2
    assert items[0].startswith("[user_decision_1]") and "double-freed" in items[0]


@pytest.mark.asyncio
async def test_a_key_served_in_many_sessions_is_one_memory_line(monkeypatch) -> None:
    # the search serves a row per writer and session: one key's rows must not fill the
    # lines, so the ask fetches per_key_fetch(3) hits and keeps one per key
    from server.memory.app.search.scoring import per_key_fetch
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    asked: list[int] = []

    def hit(key: str, session: str) -> SimpleNamespace:
        return SimpleNamespace(memory_key=key, session_id=session, content_excerpt=f"{key} text")

    async def fake_search(inp):
        asked.append(inp.top_k)
        hits = [hit("guard_reason", s) for s in ("s1", "s2", "s3")]
        hits += [hit("revert_note", "s1"), hit("lock_order", "s1"), hit("extra", "s1")]
        return SimpleNamespace(abstain=False, results=hits[: inp.top_k])

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", fake_search)
    items = await ca._inject_memory_context("why does the guard exist?")
    assert asked == [per_key_fetch(3)]
    assert [i.split("]")[0] for i in items] == ["[guard_reason", "[revert_note", "[lock_order"]


@pytest.mark.asyncio
async def test_synthesize_includes_memory_section() -> None:
    cap = _Cap()
    await ca._synthesize(cap, "why?", units=[], memories=["[user_x] chose B for safety"])
    assert "Related project memory" in cap.user
    assert "chose B for safety" in cap.user


@pytest.mark.asyncio
async def test_synthesize_no_memory_section_when_none() -> None:
    cap = _Cap()
    await ca._synthesize(cap, "why?", units=[], memories=None)
    assert "Related project memory" not in cap.user
