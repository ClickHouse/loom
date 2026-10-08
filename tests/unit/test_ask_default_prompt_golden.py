"""The synthesis prompt memory.ask sends on the default path, pinned byte for byte.

A characterization test: the scenarios below are distinct question shapes (a
set / ordering question, a sentiment-at-a-moment question, a count question,
rows that carry a speaker, the premise line on), rendered with the deployment
defaults. A refactor must leave every byte the model reads
unchanged. Regenerate the fixture with
`python -m tests.unit.test_ask_default_prompt_golden --capture` ONLY when the
default prompt changes on purpose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.ask import rendering, synthesis

GOLDEN = Path(__file__).parent / "fixtures" / "ask_default_prompt_golden.json"


class _RecordingLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(
            text="Two trips: Yosemite [t1] and the road trip [t2].", model=self.model
        )


def _hit(key: str, excerpt: str, **kw: Any) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type=kw.pop("memory_type", "semantic"),
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
        valid_at=kw.pop("valid_at", "2023-05-21 10:00"),
        **kw,
    )


def _trips() -> list[Any]:
    return [
        _hit("t1", "user went to Yosemite with friends", valid_at="2023-05-15", speaker="user"),
        _hit("t2", "user took a road trip down the coast", valid_at="2023-04-20", speaker="user"),
        _hit("t3", "user visited Yosemite again for a day", valid_at="2023-06-02"),
    ]


def _speakers() -> list[Any]:
    return [
        _hit("s1", "has three snakes and describes them as calm", speaker="jolene"),
        _hit("s2", "plays the violin every evening", speaker="deborah"),
        _hit("s3", "the assistant suggested a terrarium", speaker="assistant"),
    ]


def _moods() -> list[Any]:
    return [
        _hit("f1", "felt disappointed after the interview", valid_at="2024-10-02 07:40"),
        _hit("f2", "was excited about the trip", valid_at="2024-10-01 21:31"),
    ]


async def _run(name: str, question: str, memories: list[Any], **kw: Any) -> dict[str, str]:
    llm = _RecordingLLM()
    await synthesis._synthesize(llm, question, memories, kw.pop("max_answer_chars", 0), **kw)
    system, user = llm.calls[-1]
    return {"name": name, "system": system, "user": user}


async def scenarios() -> list[dict[str, str]]:
    order_q = "What is the order of the three trips I took, from earliest to latest?"
    set_q = "Which trips did I take this spring?"
    snake_q = "How does Deborah describe her snakes?"
    mood_q = "What is the most likely user sentiment around '2024-10-02 07:53' Wednesday?"
    count_q = "How many trips did I take in total?"
    return [
        await _run("qf_ordering", order_q, _trips()),
        await _run("qf_set", set_q, _trips()),
        await _run("qf_speaker_premise", snake_q, _speakers(), presupposition=True),
        await _run("qf_affect_moment", mood_q, _moods()),
        await _run("qf_count", count_q, _trips()),
        await _run("qf_count_capped", count_q, _trips(), max_answer_chars=300),
    ]


def _capture() -> None:
    import asyncio

    rows = asyncio.run(scenarios())
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} prompts to {GOLDEN}")


@pytest.mark.asyncio
async def test_default_prompt_bytes_match_the_golden_capture() -> None:
    golden = {r["name"]: r for r in json.loads(GOLDEN.read_text())}
    rows = await scenarios()
    assert {r["name"] for r in rows} == set(golden)
    for r in rows:
        g = golden[r["name"]]
        assert r["system"] == g["system"], f"{r['name']}: system prompt changed"
        assert r["user"] == g["user"], f"{r['name']}: user prompt changed"


def test_default_window_lines_name_no_speaker() -> None:
    _, excerpts, *_ = rendering._render_window(_speakers(), None, "what about snakes?")
    assert "said by" not in excerpts
    assert "(semantic)" in excerpts


if __name__ == "__main__":
    if "--capture" in sys.argv:
        _capture()
