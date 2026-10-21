"""A short input is extracted with the compact system prompt."""

from __future__ import annotations

import re

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import _use_compact_prompt, extract_from_messages
from server.memory.domain.extraction_prompts import (
    _COMPACT_EXAMPLE_IDS,
    _EXTRACTION_SYSTEM_PROMPT,
    compact_extraction_prompt,
)

_EXAMPLE_ID = re.compile(r"\nEXAMPLE ([0-9]+[a-z]?) — ")


def test_compact_prompt_keeps_every_rule_and_only_the_kept_examples() -> None:
    compact = compact_extraction_prompt(_EXTRACTION_SYSTEM_PROMPT)
    head = _EXTRACTION_SYSTEM_PROMPT[: _EXTRACTION_SYSTEM_PROMPT.index("\nEXAMPLE ")]
    assert compact.startswith(head)
    assert set(_EXAMPLE_ID.findall(compact)) == set(_COMPACT_EXAMPLE_IDS)
    assert len(compact) < 0.7 * len(_EXTRACTION_SYSTEM_PROMPT)
    # every kept id names an example the full prompt has
    assert _COMPACT_EXAMPLE_IDS <= set(_EXAMPLE_ID.findall(_EXTRACTION_SYSTEM_PROMPT))


def test_prompt_without_examples_is_unchanged() -> None:
    assert compact_extraction_prompt("rules only") == "rules only"


def test_gate_reads_the_current_messages_size(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_compact_prompt_max_chars", 50)
    assert _use_compact_prompt([{"role": "user", "content": "we deploy on Tuesdays"}])
    assert not _use_compact_prompt([{"role": "user", "content": "x" * 51}])
    monkeypatch.setattr(settings, "memory_extract_compact_prompt_max_chars", 0)
    assert not _use_compact_prompt([{"role": "user", "content": "hi there"}])


class _Capture:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.systems.append(system)
        return LLMResult(
            text='{"facts": []}',
            tokens_in=1,
            tokens_out=1,
            provider=self.provider,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_short_input_gets_compact_prompt_and_explicit_flag_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Capture()
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)
    msgs = [{"role": "user", "content": "ok"}]
    await extract_from_messages(msgs, judge=False, operator_policy="")
    await extract_from_messages(msgs, judge=False, compact_prompt=False, operator_policy="")
    compact, full = client.systems[0], client.systems[-1]
    assert "EXAMPLE 26" not in compact and "EXAMPLE 26" in full
    assert "STRUCTURED / ENUMERATED ASSISTANT ANSWERS" not in compact
    assert "EXAMPLE 35" in compact and "OUTPUT SCHEMA" in compact
