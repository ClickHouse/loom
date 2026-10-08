"""Block-level token accounting for the strong prompt.

The renderer reports the estimated tokens of every block of a synthesis
prompt — system, notes, window, chunks, code, chase, knowledge, raw_turns,
timeline, recent, scratchpad, question, other — as
`prompt_blocks` on the result, the wire and the ledger (`out_prompt_blocks`),
summed over the ask's synthesis-family calls. Zero behaviour change: the
prompt bytes are pinned by `fixtures/prompt_golden.json` (regenerate with
`python -m tests.unit.test_prompt_blocks --capture` ONLY when the prompt
changes on purpose).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.ask import synthesis

GOLDEN = Path(__file__).parent / "fixtures" / "prompt_golden.json"


class _RecordingLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self, text: str = "She met Samantha on 21 May 2023 [m1].") -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text=self._text, model=self.model)


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


def _pool() -> list[Any]:
    """One row of every kind the window can carry: a fact, a verbatim chunk,
    a code-plane row, a chased row, a session summary."""
    return [
        _hit("m1", "met Samantha at the beach outing, a long afternoon by the water"),
        _hit(
            "chunk:s1:3",
            "[user @ 2023-05-21 10:02] we drove to the beach and met Samantha there\n"
            "[assistant @ 2023-05-21 10:03] that sounds lovely",
            source="chunk",
            memory_type="chunk",
        ),
        _hit(
            "code_pr:ns:1",
            "PR #12 merged 2023-05-20: SystemLog prepareTable renames the old table",
            source="code_index",
            memory_type="code",
            valid_at="2023-05-20",
        ),
        _hit(
            "user_hobby",
            "likes hiking on weekends",
            explanation="identifier chase hop 1",
            valid_at="2023-04-02",
        ),
        _hit(
            "summary:s1", "Day at the beach; met Samantha.", source="summary", memory_type="summary"
        ),
    ]


_RECENT = "Recent turns:\nuser: she came along too\nassistant: who?"
_SCRATCH = "task: answer the when-question; step 2 of 3"
_TIMELINE = "2023-05-21 [m1] +0d\n2023-04-02 [user_hobby] -49d"
_KNOWLEDGE = "Knowledge passages:\n[k1] Samantha is the user's colleague since 2021."
_RAW = "[m1] user @ 2023-05-21 10:00: I met Samantha at the beach outing today."
_GRAPH = "Graph note: user_hobby precedes m1 in the digest chain."


def _common(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = dict(
        hedge_ident="PR-12",
        omitted=[_hit("m9", "an older beach day", valid_at="2022-08-01")],
        graph_note=_GRAPH,
        recent_block=_RECENT,
        scratchpad=_SCRATCH,
        timeline=_TIMELINE,
        knowledge_block=_KNOWLEDGE,
        raw_evidence=_RAW,
        presupposition=True,
        record_note=True,
        inference_note=True,
        temporal_derive=True,
        chain_note=True,
    )
    kw.update(over)
    return kw


async def _run(name: str, question: str, memories: list[Any], **kw: Any) -> dict[str, Any]:
    """One `_synthesize` call: the prompt it sent and the blocks it accounted
    (the accumulator is reset first, so `blocks` is this call's alone)."""
    from server.memory.domain import prompt_blocks as pb

    llm = _RecordingLLM()
    pb.reset()
    await synthesis._synthesize(llm, question, memories, kw.pop("max_answer_chars", 0), **kw)
    system, user = llm.calls[-1]
    return {"name": name, "system": system, "user": user, "blocks": pb.current()}


async def scenarios() -> list[dict[str, Any]]:
    """The prompts the golden pins. Every block present at least once, the
    structured and capped modes, an as-of call."""
    q = "when did she meet Samantha"
    out = [await _run("question_first_all_blocks", q, _pool(), **_common())]
    out.append(
        await _run(
            "question_first_count_options_capped",
            "How many times did she go to the beach?\nA) 1\nB) 2\nC) 3",
            _pool()[:2],
            max_answer_chars=400,
            world_knowledge=True,
            **_common(raw_evidence="", timeline=""),
        )
    )
    out.append(
        await _run(
            "question_first_as_of",
            q,
            _pool()[:3],
            as_of="2023-06-01T00:00:00",
            presupposition=True,
            knowledge_block=_KNOWLEDGE,
        )
    )
    out.append(await _run("minimal_one_row", "what does she like", _pool()[3:4]))
    return out


def _capture() -> None:
    import asyncio

    rows = [{k: r[k] for k in ("name", "system", "user")} for r in asyncio.run(scenarios())]
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} prompts to {GOLDEN}")


# ---- zero behaviour change -------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_bytes_are_identical_to_the_golden_capture() -> None:
    golden = {r["name"]: r for r in json.loads(GOLDEN.read_text())}
    rows = await scenarios()
    assert {r["name"] for r in rows} == set(golden)
    for r in rows:
        g = golden[r["name"]]
        assert r["system"] == g["system"], f"{r['name']}: system prompt changed"
        assert r["user"] == g["user"], f"{r['name']}: user prompt changed"


@pytest.mark.asyncio
async def test_an_ungrounded_identifier_skips_the_premise_line() -> None:
    """An identifier no pool row cites is a retrieval miss, not an absent
    premise: the refusal-first premise line is left out and the hedge stays."""
    marker = "FIRST LINE OF YOUR REPLY: `Premise"
    q = "when did she meet Samantha"
    grounded = await _run("grounded", q, _pool(), **_common(hedge_ident=""))
    ungrounded = await _run("ungrounded", q, _pool(), **_common())
    assert marker in grounded["system"]
    assert marker not in ungrounded["system"]
    assert "'PR-12'" in ungrounded["system"]


if __name__ == "__main__":
    if "--capture" in sys.argv:
        _capture()
