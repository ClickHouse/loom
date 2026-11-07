"""The default code.ask synthesis prompt and the answer-cache prompt version
are pinned byte for byte: the answer cache keys on
them, so an accidental change turns every cached answer stale."""

from __future__ import annotations

import asyncio

from server.code.app import answers as ca
from server.code.app import answers_entry
from server.code.domain.units import CodeUnit


class _LLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system, user, max_tokens=900):
        self.calls.append((system, user, max_tokens))

        class R:
            text = "Answer.\nCOVERAGE: full"

        return R()


def test_the_default_synthesis_prompt_is_byte_identical() -> None:
    llm = _LLM()
    unit = CodeUnit(
        unit_id="a",
        path="a.cpp",
        start_line=1,
        end_line=5,
        symbol="f",
        kind="function",
        body="int f();",
        signature="()",
    )
    asyncio.run(ca._synthesize(llm, "q", [unit], memories=["m1"], history=["h1"]))
    assert llm.calls[0][1] == (
        "Question: q\n\nCode excerpts:\n\n[a.cpp:1-5] f()\nint f();"
        "\n\nRepository history — authoritative PR / issue records from the index "
        "(answer merge state, authorship and linked issues from these; cite as [history]):\n- h1"
        "\n\nRelated project memory — decisions / rationale / prior incidents "
        "(context for WHY, not a code citation; do not cite as code):\n- m1"
    )
    assert llm.calls[0][0] == answers_entry._SYNTH_SYSTEM


def test_the_answer_cache_prompt_version_is_pinned() -> None:
    """A new value retires every cached code.ask answer on deploy — change it on purpose."""
    assert answers_entry.prompt_version() == "171b63b7b6a64522"
