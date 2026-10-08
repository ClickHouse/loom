"""The ingest describe step takes the `questions` prompt with the file card when the setting says so."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.code.app.index import describe
from server.code.app.index.ingest import Chunk


class _LLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system, user, max_tokens=60):
        self.calls.append((system, user, max_tokens))

        class R:
            text = "Does X.\nAnswers: how does the server do X?"

        return R()


def _chunk() -> Chunk:
    return Chunk(
        path="src/a.cpp",
        scope="DB::f",
        kind="function",
        start_line=1,
        end_line=3,
        content="int f() { return 1; }",
    )


def test_oneliner_prompt_is_the_default_and_unchanged() -> None:
    llm = _LLM()
    from server.settings import settings

    with patch.object(settings, "code_context_description_prompt", "oneliner"):
        out = asyncio.run(describe._describe_chunk(llm, _chunk(), card="card text"))
    system, user, max_tokens = llm.calls[0]
    assert system is describe._DESCRIBE_SYSTEM and "File card" not in user and max_tokens == 60
    assert out == "Does X."  # first line only


def test_questions_prompt_carries_the_card_and_keeps_both_sentences() -> None:
    llm = _LLM()
    from server.settings import settings

    with patch.object(settings, "code_context_description_prompt", "questions"):
        out = asyncio.run(describe._describe_chunk(llm, _chunk(), card="The file owns X."))
    system, user, max_tokens = llm.calls[0]
    assert (
        system is describe._DESCRIBE_QUESTIONS_SYSTEM
        and user.startswith("File card: The file owns X.")
        and max_tokens == 120
    )
    assert out == "Does X. Answers: how does the server do X?"


def test_a_long_chunk_is_described_from_its_head_not_skipped() -> None:
    body = "void f()\n{\n" + "    step();\n" * 600 + "}\n"
    long = Chunk(
        path="src/a.cpp", scope="DB::f", kind="function", start_line=1, end_line=603, content=body
    )
    system, user, _ = describe.describe_prompt(long)
    assert user.startswith("File: src/a.cpp\nScope: DB::f\n\nvoid f()")
    assert len(user) < describe._DESCRIBE_MAX_CHARS + 200
    assert user.endswith(f"of {body.count(chr(10)) + 1} lines)")
    llm = _LLM()
    assert asyncio.run(describe._describe_chunk(llm, long)) == "Does X."
    assert len(llm.calls) == 1
