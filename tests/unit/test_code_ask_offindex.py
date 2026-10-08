"""Identifier-proposer parsing: the NONE contract and identifier hygiene.

The prompt comment in code_answers.py explains why the proposer neither
checks symbol existence nor softens the NONE wording. These tests pin the
contract's parsing behavior.
"""

from __future__ import annotations

import pytest

from server.code.app.answers import _propose_identifiers


class _LLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, system: str, user: str, max_tokens: int = 0):
        class _R:
            text = self._text

        return _R()


@pytest.mark.asyncio
async def test_none_reply_is_the_sentinel():
    assert await _propose_identifiers(_LLM("NONE"), "q") == ["NONE"]
    assert await _propose_identifiers(_LLM("none"), "q") == ["NONE"]
    assert await _propose_identifiers(_LLM("  NONE\n"), "q") == ["NONE"]


@pytest.mark.asyncio
async def test_identifiers_parse_and_strip():
    out = await _propose_identifiers(_LLM("selectPartsToMerge, MergeTreeData `Changelog`"), "q")
    assert out == ["selectPartsToMerge", "MergeTreeData", "Changelog"]


@pytest.mark.asyncio
async def test_prose_tokens_are_dropped():
    # non-identifier junk (punctuation-bearing words) must not reach the
    # lexical leg
    out = await _propose_identifiers(_LLM("I think: maybe selectPartsToMerge?"), "q")
    assert "selectPartsToMerge?" not in out


@pytest.mark.asyncio
async def test_proposer_failure_is_empty_not_none():
    class _Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("llm down")

    assert await _propose_identifiers(_Boom(), "q") == []
