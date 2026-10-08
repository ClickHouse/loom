"""A chunk is embedded with its context — the pure parts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.app.chunk_context import (
    SYSTEM,
    build_prompt,
    clean_context,
    contextualize_chunks,
    prefixed,
)


def test_prompt_carries_document_and_chunk_and_clips_around_the_chunk():
    system, user = build_prompt("A" * 10 + " needle " + "B" * 10, "needle")
    assert system == SYSTEM and "<document>" in user and "<chunk>\nneedle\n</chunk>" in user
    long_doc = "x" * 50000 + " the chunk sits here " + "y" * 50000
    _, user2 = build_prompt(long_doc, "the chunk sits here", max_document_chars=2000)
    assert "the chunk sits here" in user2 and len(user2) < 3000


def test_clean_context_one_line_capped_prefix_stripped():
    assert clean_context("Context:  Debra and India,\n 2020-03-06,  photography") == (
        "Debra and India, 2020-03-06, photography"
    )
    assert clean_context("x" * 1000, max_chars=10) == "x" * 10
    assert clean_context("") == ""
    assert prefixed("ctx", "text") == "ctx\ntext" and prefixed("", "text") == "text"


@pytest.mark.asyncio
async def test_contextualize_chunks_is_ordered_bounded_and_fail_open():
    calls: list[str] = []

    class LLM:
        async def complete(self, system: str, user: str, max_tokens: int = 0):
            calls.append(user)
            if "boom" in user.split("<chunk>")[1]:
                raise RuntimeError("model down")
            return SimpleNamespace(
                text="Context: " + user.split("<chunk>\n")[1].split("\n")[0][:20]
            )

    out = await contextualize_chunks(LLM(), "doc", ["alpha", "boom", "gamma"], concurrency=2)
    assert out == ["alpha", "", "gamma"]
    assert len(calls) == 3
    assert await contextualize_chunks(LLM(), "doc", []) == []
