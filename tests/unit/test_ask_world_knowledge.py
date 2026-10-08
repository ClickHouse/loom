"""memory.ask world_knowledge: the prompt note only under
allow, the grounding label from the marker, and the setting's default."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.app.ask import prompts, result, synthesis


@dataclass
class _Mem:
    memory_key: str
    content_excerpt: str
    memory_type: str = "fact"
    similarity: float = 0.8


class _LLM:
    def __init__(self) -> None:
        self.system = ""
        self.user = ""

    async def complete(self, system: str, user: str, **kw: Any) -> str:
        self.system, self.user = system, user
        return SimpleNamespace(
            text="Paris is in France [general knowledge]. She moved there in May [k1]."
        )


@pytest.mark.asyncio
async def test_note_is_appended_only_under_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    mems = [_Mem("k1", "Caroline moved to Paris in May 2023.")]
    llm = _LLM()
    await synthesis._synthesize(
        llm, "Which country did Caroline move to?", mems, world_knowledge=True
    )
    assert prompts._WORLD_KNOWLEDGE_MARK in llm.system
    assert "PERSONAL fact" in llm.system
    llm2 = _LLM()
    await synthesis._synthesize(llm2, "Which country did Caroline move to?", mems)
    assert prompts._WORLD_KNOWLEDGE_MARK not in llm2.system
    # the memory-only rule is still the base of both prompts
    assert "ONLY the stored memories" in llm2.system and "ONLY the stored memories" in llm.system


def test_grounding_label() -> None:
    assert result.answer_grounding("", allow_world=True) == ""
    assert result.answer_grounding("She moved in May [k1].", allow_world=True) == "memory"
    assert (
        result.answer_grounding("Paris is in France [General Knowledge]. [k1]", allow_world=True)
        == "mixed"
    )
    assert (
        result.answer_grounding("She would likely enjoy it [inferred] [k1]", allow_world=True)
        == "mixed"
    )
    # under forbid the marker is not honoured even if a model emits it
    assert (
        result.answer_grounding("Paris is in France [general knowledge].", allow_world=False)
        == "memory"
    )


def test_setting_default_is_forbid_and_input_accepts_the_three_values() -> None:
    from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
    from server.settings import Settings

    assert Settings().memory_ask_world_knowledge == "forbid"
    base = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "query": "q",
    }
    for v in ("", "forbid", "allow"):
        assert MemoryAskInput(**base, world_knowledge=v).world_knowledge == v
    with pytest.raises(ValidationError):
        MemoryAskInput(**base, world_knowledge="maybe")
    assert MemoryAskOutput(answer="x", grounding="mixed").grounding == "mixed"
