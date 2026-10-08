"""Grounding grades: verbatim, observed, derived, unresolved."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from server.memory.domain.grounding import (
    DERIVED,
    OBSERVED,
    UNRESOLVED,
    VERBATIM,
    grade,
    grade_facts,
    normalize,
)

TURNS_RAW = [
    {"role": "user", "content": "I switched to  “WaterMinder” last week — it's great."},
    {"role": "assistant", "content": "Noted: WaterMinder for water tracking."},
]


def test_normalize_makes_quote_and_source_comparable() -> None:
    assert normalize("  “WaterMinder”  last WEEK ") == "waterminder last week"
    assert normalize("It's great.") == "its great"


def test_grade_ladder() -> None:
    turns = [normalize(t["content"]) for t in TURNS_RAW]
    assert grade("switched to WaterMinder last week", [0], turns) == (
        VERBATIM,
        "switched to waterminder last week",
    )
    # quote occurs only in an uncited turn: still verbatim (the span exists)
    assert grade("WaterMinder for water tracking", [0], turns)[0] == VERBATIM
    assert grade("the user prefers WaterMinder over other apps", [0], turns) == (
        UNRESOLVED,
        "the user prefers waterminder over other apps",
    ), "a paraphrase is kept and named for what it is"
    assert grade("", [1], turns) == (OBSERVED, "")
    assert grade("", [], turns) == (DERIVED, "")
    assert grade("", [7], turns) == (DERIVED, ""), "an out-of-range citation ties to nothing"
    assert grade("ok", [0], turns) == (OBSERVED, ""), "a two-character quote is no quote"


@dataclass(frozen=True)
class _F:
    memory_key: str
    evidence: str = ""
    source_message_indices: list[int] = field(default_factory=list)
    grounding: str = ""


def test_grade_facts_returns_graded_copies() -> None:
    facts = [
        _F("a", evidence="switched to WaterMinder", source_message_indices=[0]),
        _F("b", source_message_indices=[1]),
        _F("c"),
        _F("d", evidence="never said this", source_message_indices=[0]),
    ]
    graded = grade_facts(facts, TURNS_RAW)
    assert [f.grounding for f in graded] == [VERBATIM, OBSERVED, DERIVED, UNRESOLVED]
    assert graded[3].evidence == "never said this", "the paraphrase stays inspectable"
    assert facts[0].grounding == "", "inputs are not mutated"


def test_parser_reads_evidence_and_extractor_grades_it() -> None:
    from server.memory.app.extract.parse import _parse_fact_list

    text = (
        '{"facts":[{"key":"user_uses_waterminder","value":"User uses WaterMinder.",'
        '"entities":["waterminder"],"source_message_indices":[0],'
        '"evidence":"switched to “WaterMinder” last week"}]}'
    )
    facts = _parse_fact_list(text)
    assert facts and facts[0].evidence.startswith("switched to")
    graded = grade_facts(facts, TURNS_RAW)
    assert graded[0].grounding == VERBATIM


def test_version_row_defaults_missing_string_columns_to_empty() -> None:
    """A MemoryRow read without a grounding column (or built by a fake) has no grounding;
    re-emitting it must land '', or memory.delete fails with
    'Invalid None value in non-Nullable column grounding'."""
    from server.memory import store as mr

    base = dict(
        memory_id="m1",
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        expires_at=None,
    )
    row = mr.MemoryRow(**base)
    out = mr.build_memory_version_row(row, tombstoned_at=None)
    assert out["grounding"] == "" and out["evidence"] == ""
    assert out["verified_by"] == "" and out["source_trace"] == ""
    graded = mr.MemoryRow(**base, grounding="verbatim", evidence="switched to waterminder")
    out2 = mr.build_memory_version_row(graded)
    assert out2["grounding"] == "verbatim" and out2["evidence"] == "switched to waterminder"
