"""A call the switch holds (`customer.HOLDS`) is not a provider failure: the
extraction judge, the relation backfill and the coverage sweep let it through
instead of keeping the first pass's facts, so the extract waits for the org."""

from __future__ import annotations

from typing import Any

import pytest

from server.llm import customer
from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.judge import _judge_facts
from server.memory.app.extract.relations import _backfill_relations
from server.memory.app.extract.sweep import _coverage_sweep
from tests.unit.test_coverage_sweep import FIRST, MSGS

HELD = [
    customer.ModelCallsDisabled("model calls are switched off for org 'acme'"),
    customer.SwitchUnread("the model-call switch could not be read"),
]


class _Held:
    provider, model = "stub", "stub-1"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def complete(self, *a: Any, **k: Any) -> Any:
        raise self.error


_FACT = ExtractedFact(
    "apple_ceo",
    "The chief executive officer of Apple Inc. is Tim Cook.",
    entities=["apple inc", "tim cook"],
)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", HELD, ids=type)
async def test_the_judge_lets_a_held_call_through(error: Exception) -> None:
    with pytest.raises(type(error)):
        await _judge_facts([{"role": "user", "content": "hi"}], [_FACT], client=_Held(error))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", HELD, ids=type)
async def test_the_relation_backfill_lets_a_held_call_through(error: Exception) -> None:
    with pytest.raises(type(error)):
        await _backfill_relations([_FACT], client=_Held(error))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", HELD, ids=type)
async def test_the_coverage_sweep_lets_a_held_call_through(error: Exception) -> None:
    with pytest.raises(type(error)):
        await _coverage_sweep(
            MSGS,
            FIRST,
            base_prompt="SYS",
            operator_policy="",
            observation_date="2023-07-03",
            last_messages=None,
            max_tokens=512,
            min_chars=40,
            max_turns=60,
            client=_Held(error),
        )
