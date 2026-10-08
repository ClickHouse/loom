"""A `none` that has a same-titled record says so.

The fit's verdict and items stay the fit's; `closest` lists the scored-out rows
whose title is near-identical to the draft's, and `needs` names the inputs the
stored fit rewards that the draft left empty — read from the coefficients.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.tracker.app.dupcheck as hd
import server.tracker.app.dupcheck_fanout as fo
from server.memory.app.loom_search.tracker_leg import lean_tracker_section, tracker_text_lines
from server.tracker.api.models import TrackerDuplicateCheckInput, TrackerDuplicateCheckOutput
from server.tracker.domain import dupcheck as dc
from server.tracker.domain import dupcheck_closest as dcl
from tests.unit.test_tracker_duplicate_check import (  # noqa: F401 — _cfg is autouse
    DRAFT,
    FIT,
    NS,
    ORG,
    _cfg,
    _Src,
    _wire,
)

# a real tracker fit: frames, error_code, version and
# component pinned at 0; closeness and fix_path carry weight
LIVE_COEF = {
    "mentions": 7.322,
    "intercept": -5.879,
    "closeness": 3.487,
    "inv_rank": 2.341,
    "fix_path": 1.217,
    "lexical": -1.199,
    "agreement": 1.053,
    "legs": 0.835,
    "vector_inv_rank": 0.569,
    "gap": -0.49,
    "log_age": -0.366,
    "bm25_share": -0.039,
    "component": 0.0,
    "error_code": 0.0,
    "frames": 0.0,
    "vector": 0.0,
    "version": 0.0,
}
DRAFT_TITLE = "Logical error: Bad cast from type DB::ColumnNullable to DB::ColumnString"


def test_title_similarity_reads_the_same_title_through_quotes_and_case() -> None:
    same = "Logical error: 'Bad cast from type DB::ColumnNullable to DB::ColumnString'."
    assert dcl.title_similarity(DRAFT_TITLE, same) >= dcl.TITLE_SIMILARITY_MIN
    # the same template naming other column types is another defect
    other = "Logical Error: Bad cast from type DB::ColumnString to DB::ColumnLowCardinality"
    assert dcl.title_similarity(DRAFT_TITLE, other) < dcl.TITLE_SIMILARITY_MIN
    assert dcl.title_similarity("", same) == 0.0


def test_closest_keeps_only_near_identical_titles_most_similar_first() -> None:
    unserved = [
        {
            "number": 75593,
            "title": "Logical Error: Bad cast from type DB::ColumnString to DB::ColumnLowCardinality",
            "confidence": 0.002,
            "state": "closed",
        },
        {
            "number": 82499,
            "title": "Logical error: 'Bad cast from type DB::ColumnNullable to DB::ColumnString'.",
            "confidence": 0.012,
            "state": "closed",
            "url": "u/82499",
            "evidence": ["found by: lexical, vector", "vector: cosine distance 0.44", "x", "y"],
        },
    ]
    rows = dcl.closest_rows(unserved, DRAFT_TITLE)
    assert [r["number"] for r in rows] == [82499]
    assert rows[0]["title_similarity"] >= 0.9 and len(rows[0]["evidence"]) == 3
    assert dcl.closest_rows(unserved, DRAFT_TITLE, exclude={82499}) == []
    assert dcl.closest_rows(unserved, "") == []
    many = [{**unserved[1], "number": n} for n in range(10)]
    assert len(dcl.closest_rows(many, DRAFT_TITLE)) == dcl.CLOSEST_MAX


def test_needs_reads_the_fit_never_a_fixed_list() -> None:
    fit = {"coef": LIVE_COEF}
    bare = {"body": False, "paths": False, "frames": False, "version": False, "component": False}
    got = dcl.needs(fit, bare)
    # closeness (3.49) before fix_path (1.22); frames etc. are pinned at 0 → never asked for
    assert [n["input"] for n in got] == ["body", "paths"]
    assert dcl.needs(fit, {**bare, "body": True}) == [
        {"input": "paths", "what": "the source paths the failure touches", "feature": "fix_path"}
    ]
    weighs_frames = {"coef": {**LIVE_COEF, "frames": 2.0}}
    assert "frames" in [n["input"] for n in dcl.needs(weighs_frames, bare)]
    assert dcl.needs(None, bare) == []


def test_note_names_the_record_the_score_and_what_to_pass() -> None:
    rows = [{"number": 82499, "state": "closed", "confidence": 0.012}]
    need = [{"input": "body"}, {"input": "paths"}]
    text = dcl.note(rows, need, 0.05)
    assert "#82499, closed" in text and "0.012 < related 0.05" in text
    assert "`body`, `paths`" in text
    assert dcl.note([], need, 0.05) == ""


def _weak_fit(base: TrackerDuplicateCheckOutput) -> dict[str, Any]:
    f = base.items[0]["features"]
    logit = sum(FIT["coef"][k] * f[k] for k in dc.FEATURE_NAMES[1:])
    return {**FIT, "coef": {**FIT["coef"], "intercept": -logit - 3.0}}  # every row < related


@pytest.mark.asyncio
async def test_a_none_with_a_same_titled_record_carries_it_and_keeps_the_verdict(
    monkeypatch,
) -> None:
    s = _Src()
    _wire(monkeypatch, s)
    base = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    _wire(monkeypatch, s, _weak_fit(base))
    # the draft carries #80550's exact title (the stub's second candidate), no body
    same = "avgWeighted rounds float value when weight is int"
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, title=same)
    )
    assert out.verdict == "none" and out.items == []  # the fit's answer, untouched
    assert [r["number"] for r in out.closest] == [80550]
    assert out.closest[0]["title_similarity"] == 1.0 and out.closest[0]["state"] == "open"
    assert "#80550" in out.closest_note and "did not call it a duplicate" in out.closest_note
    # this fit weighs fix_path (2.0) and version/error_code (0.5): the draft had none of them
    assert "paths" in [n["input"] for n in out.needs]


@pytest.mark.asyncio
async def test_no_same_title_means_no_closest_and_the_old_shape(monkeypatch) -> None:
    s = _Src()
    _wire(monkeypatch, s)
    base = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    _wire(monkeypatch, s, _weak_fit(base))
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert (
        out.verdict == "none" and out.closest == [] and out.needs == [] and out.closest_note == ""
    )
    # a duplicate answer never carries closest
    _wire(monkeypatch, s)
    dup = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(
            org=ORG, namespace=NS, **DRAFT, component="AggregateFunctions", version="25.8.3"
        )
    )
    assert dup.verdict == "duplicate_of" and dup.closest == []


def _out(
    verdict: str, closest: list[dict[str, Any]], needs: list[dict[str, str]] | None = None
) -> Any:
    return TrackerDuplicateCheckOutput(
        verdict=verdict,
        closest=closest,
        needs=needs or [],
        thresholds={"duplicate": 0.36, "related": 0.05},
    )


def test_fanout_merges_closest_across_namespaces_and_drops_it_on_a_duplicate() -> None:
    a = _out(
        "none",
        [{"number": 1, "title_similarity": 0.95, "confidence": 0.01, "state": "closed"}],
        [{"input": "body", "what": "w", "feature": "closeness"}],
    )
    b = _out(
        "none",
        [
            {"number": 2, "title_similarity": 1.0, "confidence": 0.02},
            {"number": 3, "title_similarity": 0.92, "confidence": 0.0},
            {"number": 4, "title_similarity": 0.91, "confidence": 0.0},
        ],
    )
    m = fo.merge_outputs(["qa-a", "qa-b"], [a, b], 10)
    assert m.verdict == "none" and [r["number"] for r in m.closest] == [2, 1, 3]
    assert m.closest[0]["namespace"] == "qa-b" and m.needs[0]["input"] == "body"
    assert "#2" in m.closest_note
    dup = _out("duplicate_of", [])
    dup.items = [{"number": 9, "relation": "duplicate", "confidence": 0.9}]
    m2 = fo.merge_outputs(["qa-a", "qa-b"], [dup, b], 10)
    assert m2.verdict == "duplicate_of" and m2.closest == []


def test_loom_search_section_says_the_title_is_filed_instead_of_file_it() -> None:
    out = _out(
        "none",
        [
            {
                "number": 82499,
                "title": "Logical error: 'Bad cast …'",
                "state": "closed",
                "confidence": 0.012,
                "title_similarity": 1.0,
                "url": "u",
                "evidence": ["x"],
            }
        ],
    )
    out.closest_note = "same title as #82499, closed; the fit did not call it a duplicate"
    section = lean_tracker_section(out, {"signals": ["error", "intent"]})
    assert section["results"] == [] and section["closest"][0]["number"] == 82499
    assert (
        "evidence" not in section["closest"][0] and "title_similarity" not in section["closest"][0]
    )
    text = "\n".join(tracker_text_lines(section))
    assert (
        "it can be filed" not in text and "same title is already filed" in text and "#82499" in text
    )
    plain = lean_tracker_section(_out("none", []), {"signals": []})
    assert "closest" not in plain
    assert "no likely duplicate in the indexed records" in "\n".join(tracker_text_lines(plain))
