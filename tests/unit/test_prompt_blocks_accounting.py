"""The per-block token accounting — complete against the bytes
sent, attributed by row kind, summed over an ask's synthesis calls, on the
result, the wire and the ledger. The prompt bytes themselves are pinned by
`test_prompt_blocks.py`; its scenarios return each call's blocks."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from server.memory.api.models_ask import MemoryAskOutput
from server.memory.app.ask import rendering
from server.memory.domain import prompt_blocks as pb
from server.observability.event_args import IDENTIFIER_KEYS, project_result
from tests.unit import test_prompt_blocks as golden

_EVERY_BLOCK = (
    "system",
    "notes",
    "window",
    "chunks",
    "code",
    "chase",
    "knowledge",
    "raw_turns",
    "timeline",
    "recent",
    "scratchpad",
    "elision",
    "graph",
    "question",
)


def _total_tokens(system: str, user: str) -> int:
    return (len(system) + len(user)) // pb.CHARS_PER_TOKEN


# ---- the pure module -------------------------------------------------------------


def test_row_block_by_kind() -> None:
    assert pb.row_block(SimpleNamespace(memory_key="code_pr:ns:1", source="code_index")) == "code"
    chased = SimpleNamespace(memory_key="k", source="fact", explanation="identifier chase hop 2")
    assert pb.row_block(chased) == "chase"
    assert pb.row_block(SimpleNamespace(memory_key="chunk:s1:3", source="chunk")) == "chunks"
    assert pb.row_block(SimpleNamespace(memory_key="chunk:s1:3", source="")) == "chunks"
    assert pb.row_block(SimpleNamespace(memory_key="summary:s1", source="summary")) == "window"
    assert pb.row_block(SimpleNamespace(memory_key="user_a", source="fact")) == "window"


def test_block_tokens_are_complete_and_other_never_negative() -> None:
    out = pb.block_tokens(total_chars=1000, parts={"system": 400, "window": 400, "recent": 0})
    assert out == {"system": 100, "window": 100, "other": 50}
    assert list(out) == ["system", "window", "other"], "canonical order"
    # A layout that renders a row twice cannot claim more than the prompt holds.
    assert pb.block_tokens(total_chars=100, parts={"window": 400}) == {"window": 100}


def test_accumulation_sums_calls_and_reset_clears() -> None:
    pb.reset()
    assert pb.current() == {}
    pb.add({"system": 10, "window": 20})
    pb.add({"system": 10, "raw_turns": 5})
    assert pb.current() == {"system": 20, "window": 20, "raw_turns": 5}
    pb.reset()
    assert pb.current() == {}


# ---- the renderer's accounting against the bytes it sent ---------------------------


@pytest.mark.asyncio
async def test_every_scenario_sums_to_the_prompt_and_attributes_over_95_percent() -> None:
    """For each golden scenario the blocks sum to the prompt's chars/4 (within
    one token per block of floor rounding) and `other` — heads, separators,
    the attribution line — stays under the 5% gate."""
    rows = await golden.scenarios()
    assert rows
    for r in rows:
        blocks, total = r["blocks"], _total_tokens(r["system"], r["user"])
        assert blocks, r["name"]
        assert abs(sum(blocks.values()) - total) <= len(blocks), (r["name"], blocks, total)
        assert blocks.get("other", 0) <= 0.05 * total, (r["name"], blocks, total)
        assert blocks["system"] > 0 and blocks["question"] > 0, (r["name"], blocks)


@pytest.mark.asyncio
async def test_rows_are_attributed_by_kind_and_context_blocks_by_name() -> None:
    rows = {r["name"]: r for r in await golden.scenarios()}
    b = rows["question_first_all_blocks"]["blocks"]
    for name in _EVERY_BLOCK:
        assert b.get(name, 0) > 0, (name, b)
    # Two window rows (a fact, a summary) outweigh the one chunk; the chased row is smallest.
    assert b["window"] > b["chunks"] > b["chase"]
    # The context blocks are their texts at chars/4 (heads included where rendered).
    assert b["knowledge"] == len(golden._KNOWLEDGE) // 4
    assert b["scratchpad"] == (len(rendering._SCRATCHPAD_HEAD) + len(golden._SCRATCH)) // 4
    assert b["raw_turns"] == (len(rendering._SOURCE_TURNS_HEAD) + len(golden._RAW)) // 4
    # A bare call: system, one window row, the question — nothing else.
    m = rows["minimal_one_row"]["blocks"]
    assert set(m) <= {"system", "notes", "window", "chase", "question", "other"}, m


# ---- the ask: summed over its synthesis calls, on the result, wire and ledger -----


@pytest.mark.asyncio
async def test_two_call_ask_reports_the_sum_and_the_wire_and_ledger_carry_it(monkeypatch) -> None:
    from tests.unit import test_ask_synth_accounting as ef_mod

    env = ef_mod.make_env(monkeypatch)
    out = await ef_mod._ask(depth="raw")
    assert out.source == "synthesized" and out.synth_calls == 2
    first, deep = env.llm.synth_calls()
    total = _total_tokens(first[0], first[1]) + _total_tokens(deep[0], deep[1])
    blocks = out.prompt_blocks
    assert blocks and abs(sum(blocks.values()) - total) <= 2 * len(blocks), (blocks, total)
    assert blocks["raw_turns"] > 0, "the deep call's source turns are accounted"
    assert blocks.get("other", 0) <= 0.05 * total
    # The wire model carries the map; the ledger row carries it as one key.
    assert "prompt_blocks" in MemoryAskOutput.model_fields and "prompt_blocks" in IDENTIFIER_KEYS
    row = project_result(out)
    parsed = dict(kv.split("=") for kv in row["out_prompt_blocks"].split(","))
    assert {k: int(v) for k, v in parsed.items()} == blocks
    # JSON round trip for the SDK.
    assert json.loads(out.model_dump_json())["prompt_blocks"] == blocks


@pytest.mark.asyncio
async def test_a_second_ask_starts_from_zero(monkeypatch) -> None:
    from tests.unit import test_ask_synth_accounting as ef_mod

    ef_mod.make_env(monkeypatch)
    first = await ef_mod._ask(depth="excerpt")
    second = await ef_mod._ask(depth="excerpt")
    assert first.synth_calls == second.synth_calls == 1
    assert first.prompt_blocks == second.prompt_blocks, "not accumulated across asks"


def test_an_unsynthesized_answer_reports_no_blocks() -> None:
    out = MemoryAskOutput(answer="", source="abstained")
    assert out.prompt_blocks == {}
    assert "out_prompt_blocks" not in project_result(out)


def test_numeric_map_projection_rules() -> None:
    from server.observability.event_args import _identifier

    assert _identifier({"b": 2, "a": 2, "c": 10}) == "c=10,a=2,b=2"
    long = _identifier({f"b{i:02d}": 10_000 + i for i in range(60)})
    assert long is not None and len(long) == 400 and long.endswith("…"), "cut at the map bound"
    assert _identifier({"a": 1.5}) == "a=1.5"
    assert _identifier({}) is None
    assert _identifier({"a": "text"}) is None, "a map with text is not an identifier"
    assert _identifier({"a": True}) is None
    assert _identifier({"a": {"b": 1}}) is None
