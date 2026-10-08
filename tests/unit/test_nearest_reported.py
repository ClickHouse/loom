"""A `none` shows the nearest on-topic issues under the line.

"IllegalTypeOfArgument in toStartOfInterval — already reported?" must not
answer a bare `none` while on-topic `toStartOfInterval` issues sit just under
the related line.
"""

from __future__ import annotations

from typing import Any

from server.memory.app.loom_search import tracker_leg as ql
from server.memory.domain.loom_output import is_loom_output
from server.tracker.api.models import TrackerDuplicateCheckOutput
from server.tracker.app import dupcheck_answer as td_answer
from server.tracker.app.dupcheck_fanout import merge_outputs
from server.tracker.domain import dupcheck_closest as dcl

DRAFT = "IllegalTypeOfArgument in toStartOfInterval"


def _row(n: int, title: str, conf: float, **kw: Any) -> dict[str, Any]:
    return {
        "kind": "issue",
        "number": n,
        "title": title,
        "state": "open",
        "url": f"https://g/{n}",
        "confidence": conf,
        "why": ["lexical", "vector"],
        "evidence": ["e1"],
        "features": {"vector_inv_rank": 0.2},
        **kw,
    }


# the live unserved rows for DRAFT (confidence from the stored fit)
UNSERVED = [
    _row(121114, "toStartOfInterval WEEK bucket for Date saturates to 1970-01-01", 0.0048),
    _row(55784, "tumble and toStartOfInterval : argument 1 must be constant", 0.0046),
    _row(119969, "`roundDown` skips intra-day offset change: `toStartOfHour` gives :30", 0.0020),
    _row(
        112184, "UndefinedBehaviorSanitizer: signed integer overflow in `toStartOfInterval`", 0.0016
    ),
    _row(59649, "Regarding WITH FILL usage", 0.0014),
    _row(77746, '`toStartOfInterval` errors with "The origin must be before the end date"', 0.0013),
    _row(71941, "24.7+: Broken query by partition pruning", 0.0011),
]


def test_nearest_keeps_on_topic_rows_by_confidence_trimmed_to_one_line() -> None:
    rows = dcl.nearest_rows(UNSERVED, DRAFT)
    assert [r["number"] for r in rows] == [121114, 55784, 112184]
    assert set(rows[0]) == {"number", "title", "state", "url", "confidence", "why"}
    assert "evidence" not in rows[0] and "features" not in rows[0], (
        "one line's worth, not the fit's bookkeeping"
    )
    assert dcl.nearest_rows(UNSERVED, DRAFT, exclude={121114}, limit=1)[0]["number"] == 55784
    assert dcl.nearest_rows(UNSERVED, DRAFT, limit=0) == [] and dcl.nearest_rows(UNSERVED, "") == []
    prs = [_row(5, "toStartOfInterval fix", 0.9, kind="pr")]
    assert dcl.nearest_rows(prs, DRAFT) == [], "issues only: a PR is not a reported defect"


def test_on_topic_needs_an_identifier_when_the_draft_has_one() -> None:
    assert dcl.on_topic(DRAFT, "Illegal type of argument for function x"), (
        "the squashed error name counts"
    )
    assert not dcl.on_topic(DRAFT, "Regarding WITH FILL usage")
    nullable = "Logical error: Bad cast from type DB::ColumnNullable to DB::ColumnString"
    assert dcl.on_topic(nullable, "Bad cast from ColumnNullable to ColumnVector"), (
        "`DB::` is optional in a title"
    )
    assert not dcl.on_topic(nullable, "Logical error: Bad cast from type A to B"), (
        "generic words are not a topic"
    )
    plain = "Server crashes when reading parquet with nested arrays"
    assert dcl.on_topic(plain, "Nested arrays in Parquet lose rows")
    assert not dcl.on_topic(plain, "Server crashes when reading")


class _Q:
    frames: list[str] = []
    versions: list[str] = []
    component = ""


class _In:
    title = DRAFT
    body = ""
    org = "clickhouse"
    namespace = "qa-clickhouse"


def _out(verdict: str, items: list[dict[str, Any]] | None = None) -> TrackerDuplicateCheckOutput:
    return TrackerDuplicateCheckOutput(
        verdict=verdict, items=items or [], thresholds={"duplicate": 0.36, "related": 0.05}
    )


def test_the_handler_adds_nearest_on_none_and_fills_related_up_to_three() -> None:
    run = {"unserved": UNSERVED, "q": _Q(), "draft_paths": []}
    none = td_answer.with_closest(_out("none"), run, _In(), None)
    assert none.verdict == "none" and none.items == [], "the verdict and items stay the fit's"
    assert [r["number"] for r in none.nearest] == [121114, 55784, 112184]
    served = [_row(121114, "toStartOfInterval WEEK", 0.2, relation="related")]
    rel = td_answer.with_closest(_out("related", served), run, _In(), None)
    assert [r["number"] for r in rel.nearest] == [55784, 112184], (
        "three rows shown in all, a served one not repeated"
    )
    full = [_row(n, "x", 0.2, relation="related") for n in (1, 2, 3)]
    assert td_answer.with_closest(_out("related", full), run, _In(), None).nearest == []
    dup = td_answer.with_closest(_out("duplicate_of", served), run, _In(), None)
    # A duplicate answer shows the best scored-out issue too
    assert [r["number"] for r in dup.nearest] == [55784, 112184], "runner-ups beside a duplicate"
    assert dup.verdict == "duplicate_of" and dup.items == served


def test_the_fanout_merges_nearest_by_confidence_and_names_the_namespace() -> None:
    a = _out("none").model_copy(update={"nearest": dcl.nearest_rows(UNSERVED[:2], DRAFT)})
    b = _out("none").model_copy(
        update={"nearest": dcl.nearest_rows([_row(9, "toStartOfInterval private", 0.0047)], DRAFT)}
    )
    merged = merge_outputs(["qa-clickhouse", "qa-example-private"], [a, b], 5)
    assert [(r["namespace"], r["number"]) for r in merged.nearest] == [
        ("qa-clickhouse", 121114),
        ("qa-example-private", 9),
        ("qa-clickhouse", 55784),
    ]
    rel = _out("related", [_row(1, "x", 0.2, relation="related")])
    two = merge_outputs(["qa-clickhouse", "qa-example-private"], [rel, b], 5)
    assert len(two.items) + len(two.nearest) <= dcl.NEAREST_MAX


def test_the_search_leg_shows_nearest_labelled_as_not_duplicates() -> None:
    out = _out("none").model_copy(update={"nearest": dcl.nearest_rows(UNSERVED, DRAFT)})
    sec = ql.lean_tracker_section(out, {"signals": ["error", "intent"]})
    assert sec["results"] == [] and [r["number"] for r in sec["nearest"]] == [121114, 55784, 112184]
    assert "NOT duplicates" in sec["nearest_note"]
    lines = ql.tracker_text_lines(sec)
    assert "nearest reported issues, below the related line (NOT duplicates" in lines[0]
    assert "it can be filed" not in lines[0]
    assert (
        lines[1]
        == "  - #121114 toStartOfInterval WEEK bucket for Date saturates to 1970-01-01 (open, 0.005)"
    )
    assert set(sec["nearest"][0]) == {"number", "title", "state", "confidence"}, "no url, no legs"
    bare = ql.tracker_text_lines(ql.lean_tracker_section(_out("none"), {}))
    assert "no likely duplicate in the indexed records" in bare[
        0
    ] and "nearest" not in ql.lean_tracker_section(_out("none"), {})


def test_the_nearest_header_is_loom_output_so_capture_never_quotes_it_back() -> None:
    assert is_loom_output(
        "… no duplicate (verdict none); nearest reported issues, below the related line …"
    )
