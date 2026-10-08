"""Memory.ask reads the knowledge plane as a labelled block."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
from server.memory.app.ask.context_blocks import knowledge_block_text, knowledge_passages


def _hit(src: str, idx: int, content: str, title: str = "") -> SimpleNamespace:
    return SimpleNamespace(source_uri=src, chunk_idx=idx, content=content, title=title, section="")


def test_block_renders_labelled_bounded_passages() -> None:
    assert knowledge_block_text([]) == ""
    assert knowledge_block_text([_hit("a.md", 0, "   ")]) == ""
    block = knowledge_block_text(
        [
            _hit("docs/a.md", 3, "Frank Zappa died in Los Angeles.", title="Biography"),
            _hit("docs/b.md", 0, "x" * 2000),
        ]
    )
    lines = block.split("\n")
    assert lines[0].startswith("KNOWLEDGE PASSAGES")
    assert lines[1] == "[knowledge:docs/a.md#3] (Biography) Frank Zappa died in Los Angeles."
    assert lines[2].startswith("[knowledge:docs/b.md#0] ") and lines[2].endswith("…[truncated]")
    assert len(lines[2]) < 900
    many = knowledge_block_text([_hit("d.md", i, "y" * 700) for i in range(40)])
    assert (
        len(many) <= 6000 + 80
        and "[knowledge:d.md#0]" in many
        and "[knowledge:d.md#39]" not in many
    )


@pytest.mark.asyncio
async def test_passages_follow_the_switch_and_fail_open(monkeypatch) -> None:

    si = SimpleNamespace(org="o", namespace="n", agent="*", query="q")
    assert await knowledge_passages(si, "q", enabled=False) == ("", 0)

    async def boom(_inp):
        raise RuntimeError("plane down")

    import server.knowledge.app.knowledge_search as kh

    monkeypatch.setattr(kh, "handle_knowledge_search", boom)
    assert await knowledge_passages(si, "q", enabled=True) == ("", 0)

    async def ok(inp):
        assert inp.namespace == "n" and inp.query == "q" and inp.namespace_scope == "this"
        return SimpleNamespace(results=[_hit("a.md", 1, "A passage.")])

    monkeypatch.setattr(kh, "handle_knowledge_search", ok)
    block, n = await knowledge_passages(si, "q", enabled=True)
    assert n == 1 and "[knowledge:a.md#1] A passage." in block


def test_request_and_output_fields() -> None:
    assert MemoryAskInput(org="o", namespace="n", agent="*", query="q").knowledge is None
    assert (
        MemoryAskInput(org="o", namespace="n", agent="*", query="q", knowledge=True).knowledge
        is True
    )
    assert MemoryAskOutput().knowledge_hits == 0
