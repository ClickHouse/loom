"""code.ask reverse enrichment: fuse decision-memories into the code answer,
gated by an explicit memory-namespace binding (no-op when unset)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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
    assert await ca._inject_memory_context("why is X like this?") == ca.MemoryContext()


@pytest.mark.asyncio
async def test_returns_decision_memories_when_bound(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)

    async def fake_search(inp):
        # The enrichment imports its wire model and handler inside a fail-open
        # try: a broken import would answer [] here, not raise.
        assert isinstance(inp, MemorySearchInput)
        assert inp.namespace == "support-bot"
        assert inp.namespace_scope == "this"  # whatever the deployment's default scope
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

    memory = await ca._inject_memory_context("why does the guard exist?")
    assert memory.namespace == "support-bot"
    items = memory.items
    assert len(items) == 2
    assert items[0][0] == "user_decision_1" and "double-freed" in items[0][1]


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
    items = (await ca._inject_memory_context("why does the guard exist?")).items
    assert asked == [per_key_fetch(3)]
    assert [key for key, *_ in items] == ["guard_reason", "revert_note", "lock_order"]


@pytest.mark.asyncio
async def test_a_hit_from_another_namespace_is_never_shown(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)

    async def fake_search(inp):
        return SimpleNamespace(
            abstain=False,
            results=[
                SimpleNamespace(
                    memory_key="private_note", namespace="alice-private", content_excerpt="x"
                ),
                SimpleNamespace(memory_key="guard_reason", namespace="", content_excerpt="y"),
                SimpleNamespace(
                    memory_key="lock_order", namespace="support-bot", content_excerpt="z"
                ),
            ],
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", fake_search)
    memory = await ca._inject_memory_context("why does the guard exist?")
    assert [(key, text) for key, text, _ in memory.items] == [
        ("guard_reason", "y"),
        ("lock_order", "z"),
    ]


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


def _hit(key: str, source: str, parent: str = "", tags: Any = ()) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        source=source,
        parent_memory_key=parent,
        tags=None if tags is None else list(tags),
        content_excerpt=f"{key} text",
    )


@pytest.mark.parametrize(
    ("hit", "key"),
    [
        (_hit("user_decision", "fact"), "user_decision"),
        # a consolidated row is written from other memories: a redact of a source does
        # not reach its key, so it names none
        (_hit("merged_fact", "fact", tags=("consolidated", "src:user_decision")), ""),
        (_hit("derived_fact", "fact", tags=("src:summary:s1",)), ""),
        (_hit("untagged_fact", "fact", tags=None), ""),  # tags not read: fail closed
        (_hit("chunk:s1:0", "chunk", parent="user_rationale"), "user_rationale"),
        (_hit("chunk:s2:4", "chunk"), ""),
        (_hit("summary:s1", "summary"), ""),
        (_hit("entity:guard", "entity_summary"), ""),
    ],
)
def test_the_erasure_key_of_each_kind_of_hit(hit: SimpleNamespace, key: str) -> None:
    assert ca._erasure_key(hit, tags_read=True) == key
    assert ca._erasure_key(hit, tags_read=False) == (key if hit.source == "chunk" else "")


@pytest.mark.asyncio
async def test_the_prompt_label_is_the_hit_key_and_the_stamp_its_erasure_key(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)

    async def fake_search(inp):
        assert inp.include_tags is True  # the consolidated check reads them
        return SimpleNamespace(
            abstain=False,
            results=[
                _hit("user_decision", "fact"),
                _hit("chunk:s1:0", "chunk", parent="user_rationale"),
                _hit("summary:s1", "summary"),
            ],
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", fake_search)
    memory = await ca._inject_memory_context("why does the guard exist?")
    assert [label for label, *_ in memory.items] == ["user_decision", "chunk:s1:0", "summary:s1"]
    assert [key for *_, key in memory.items] == ["user_decision", "user_rationale", ""]


@pytest.mark.asyncio
async def test_a_fact_whose_tags_the_search_could_not_read_names_no_erasure_key(
    monkeypatch,
) -> None:
    # a failed tag read leaves [] on the hit, which reads like an untagged fact: the
    # search's `tags_unread` says the [] is unknown, so a consolidated row is not stamped
    # with a key no user erasure reaches
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)

    async def fake_search(inp):
        return SimpleNamespace(
            abstain=False,
            results=[_hit("merged_fact", "fact"), _hit("chunk:s1:0", "chunk", parent="note")],
            retrieval_work={"tags_unread": 1},
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", fake_search)
    memory = await ca._inject_memory_context("why does the guard exist?")
    assert [key for *_, key in memory.items] == ["", "note"]
