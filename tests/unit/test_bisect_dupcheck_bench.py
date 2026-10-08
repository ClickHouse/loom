"""The benchmark's failure classes, each pinned by a small
frozen fixture taken from a real tracker record."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.code.domain.index import fix_blame as fb
from server.tracker.domain import bisect as bi
from server.tracker.domain import dupcheck_closest as dcl
from server.tracker.domain import similar as sim

# ---- frames from a pasted ClickHouse trace ---------------------------------

SERVER_LOG = """<Fatal> : Logical error: 'begin <= end'.
1. /home/ubuntu/actions-runner/_work/ClickHouse/ClickHouse/src/Common/Exception.cpp:53: DB::abortOnFailedAssertion(String const&) @ 0x0000000011c0a255
2. /home/ubuntu/actions-runner/_work/ClickHouse/ClickHouse/src/Storages/MergeTree/MarkRange.cpp:19: DB::MarkRange::MarkRange(unsigned long, unsigned long) @ 0x00000000184e5bf3
3. /home/ubuntu/actions-runner/_work/ClickHouse/ClickHouse/src/Storages/MergeTree/MergeTreeDataSelectExecutor.cpp:1200: DB::MergeTreeDataSelectExecutor::filterMarksUsingIndex(int) @ 0x0000000018522a31
"""
SANITIZER = """==1==ERROR: AddressSanitizer: heap-use-after-free
    #0 0x55d1c0 in DB::ColumnDynamic::insertFrom(DB::IColumn const&, unsigned long) /src/Columns/ColumnDynamic.cpp:212
    #1 0x55d1c1 in DB::FunctionHas::executeImpl() /src/Functions/array/has.cpp:88
    #2 0x55d1c2 in DB::IFunction::execute() /src/Functions/IFunction.cpp:40
"""
CI_BLOCK = """Stack trace:
__sanitizer::LargeMmapAllocator<>::GetBlockBegin(void const*) const @ 0x000000001c465289
DB::SerializationTuple::enumerateStreams(DB::ISerialization::EnumerateStreamsSettings&) const @ 0x000000001c465300
DB::ISerialization::enumerateStreams(DB::ISerialization::SubstreamPath&) const @ 0x000000001c465400
StackTrace::tryCapture() @ 0x000000001c465500
"""


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (SERVER_LOG, "MarkRange"),
        (SANITIZER, "insertFrom"),
        (CI_BLOCK, "enumerateStreams"),
    ],
)
def test_every_clickhouse_trace_shape_feeds_the_frames_leg(text: str, want: str) -> None:
    frames = sim.frames_from_text(text)
    tokens = [f.token for f in frames]
    assert want in tokens, tokens
    # the crash plumbing and the sanitizer runtime never stand as frames
    assert not {"abortOnFailedAssertion", "tryCapture", "StackTrace"} & set(tokens)
    assert not any(f.kind == "file" and f.token in ("Exception", "StackTrace") for f in frames)


def test_prose_naming_a_symbol_is_still_not_a_trace() -> None:
    prose = "We call DB::MarkRange::MarkRange in the reader.\nAlso DB::Foo::bar.\nThat is all."
    assert sim.frames_from_text(prose) == []


def test_a_part_name_is_not_a_symbol() -> None:
    lines = ["0. a5b308ea49b::all_0_11_1_4 @ 0x0000000011c0a255"]
    assert [f for f in sim.normalise_frames(lines) if f.kind == "symbol"] == []


# ---- nearest: the semantic and trace legs' first row keeps a slot ----------


def _row(n: int, title: str, conf: float) -> dict[str, Any]:
    return {"kind": "issue", "number": n, "title": title, "confidence": conf}


def test_the_vector_legs_first_row_is_nearest_even_off_topic_by_words() -> None:
    # #88996 "A case-insensitive filesystem on Mac OS leads to problems" was a
    # duplicate of #82851 "Table corruption when importing many rows with JSON";
    # the vector leg ranked it #1, the word filter dropped it
    draft = "A case-insensitive filesystem on Mac OS leads to problems"
    unserved = [
        _row(70287, "Case-insensitive column names on macOS", 0.02),
        _row(82851, "Table corruption when importing many rows with JSON at once", 0.005),
    ]
    out = dcl.nearest_rows(unserved, draft, legs_top={"vector": [82851, 75398]})
    assert [r["number"] for r in out][:1] == [82851]
    # without the leg's order the word filter decides, as before
    assert 82851 not in [r["number"] for r in dcl.nearest_rows(unserved, draft)]


def test_a_served_or_excluded_leader_is_not_repeated() -> None:
    unserved = [_row(1, "alpha beta gamma", 0.2)]
    out = dcl.nearest_rows(unserved, "alpha beta", exclude={1}, legs_top={"vector": [1]})
    assert out == []


# ---- the canonical issue of a shown duplicate -------------------------------


class _Src:
    def __init__(self, gold: list[dict[str, Any]], items: dict[int, dict[str, Any]]) -> None:
        self.gold, self.items = gold, items

    async def duplicate_gold(self, org: str, ns: str, *, limit: int = 500) -> list[dict[str, Any]]:
        return self.gold

    async def items_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]:
        return {n: self.items[n] for n in numbers if n in self.items}


def _out(nearest: list[dict[str, Any]], items: list[dict[str, Any]] | None = None) -> Any:
    from server.tracker.api.models import TrackerDuplicateCheckOutput

    return TrackerDuplicateCheckOutput(verdict="none", items=items or [], nearest=nearest)


def _input(as_of: str = "") -> Any:
    from server.tracker.api.models import TrackerDuplicateCheckInput

    return TrackerDuplicateCheckInput(
        org="clickhouse", namespace="qa-clickhouse", title="x", as_of=as_of
    )


def test_a_shown_duplicate_brings_its_canonical_issue() -> None:
    from server.tracker.app.dupcheck_answer import with_canonical

    # #86656 → nearest #86622, itself closed as a duplicate of #86434
    src = _Src(
        [{"number": 86622, "targets": [86434], "created_at": datetime(2025, 9, 3)}],
        {
            86434: {
                "title": "ClickHouse 25.8.1.5101 Keeper exception",
                "created_at": datetime(2025, 8, 31),
            }
        },
    )
    out = asyncio.run(with_canonical(src, _input(), _out([{"number": 86622, "title": "slow"}])))
    extra = [r for r in out.nearest if r.get("canonical_of")]
    assert extra == [
        {"number": 86434, "canonical_of": 86622, "title": "ClickHouse 25.8.1.5101 Keeper exception"}
    ]


def test_a_canonical_filed_after_the_replay_wall_is_not_added() -> None:
    from server.tracker.app.dupcheck_answer import with_canonical

    src = _Src(
        [{"number": 2, "targets": [3]}],
        {3: {"title": "later", "created_at": datetime(2026, 1, 2)}},
    )
    out = asyncio.run(with_canonical(src, _input("2026-01-01 00:00:00"), _out([{"number": 2}])))
    assert not any(r.get("canonical_of") for r in out.nearest)


def test_the_canonical_step_fails_open() -> None:
    from server.tracker.app.dupcheck_answer import with_canonical

    class Broken(_Src):
        async def duplicate_gold(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("down")

    before = _out([{"number": 2}])
    assert asyncio.run(with_canonical(Broken([], {}), _input(), before)) == before


# ---- blame of the fix's own lines ---------------------------------------

# the fix of #107942 (#107997), pre-image hunks as `git diff -U0` prints them
FIX_DIFF = """diff --git a/src/DataTypes/DataTypesCache.cpp b/src/DataTypes/DataTypesCache.cpp
--- a/src/DataTypes/DataTypesCache.cpp
+++ b/src/DataTypes/DataTypesCache.cpp
@@ -103,6 +102,0 @@ SerializationPtr SimpleDataTypesCache::getSerialization(const String & type_name
@@ -111 +105,2 @@ const SimpleDataTypesCache & getSimpleDataTypesCache()
@@ -116,2 +111,2 @@ DataTypePtr DataTypesCache::getType(const String & type_name)
diff --git a/tests/queries/0_stateless/04000_x.sql b/tests/queries/0_stateless/04000_x.sql
--- a/tests/queries/0_stateless/04000_x.sql
+++ b/tests/queries/0_stateless/04000_x.sql
@@ -1,3 +1,3 @@
diff --git a/src/DataTypes/DataTypesCache.h b/src/DataTypes/DataTypesCache.h
--- a/src/DataTypes/DataTypesCache.h
+++ b/src/DataTypes/DataTypesCache.h
@@ -51,0 +50,2 @@ private:
"""


def test_pre_image_ranges_are_the_rewritten_source_lines() -> None:
    got = fb.pre_image_ranges(FIX_DIFF)
    assert [(r.path, r.start, r.end, r.weight) for r in got] == [
        ("src/DataTypes/DataTypesCache.cpp", 103, 108, 1.0),
        ("src/DataTypes/DataTypesCache.cpp", 116, 117, 1.0),
        ("src/DataTypes/DataTypesCache.cpp", 111, 111, 1.0),
        ("src/DataTypes/DataTypesCache.h", 51, 51, fb.INSERTION_WEIGHT),
    ]


def test_a_deleted_file_is_blamed_by_its_old_path() -> None:
    diff = "--- a/src/Old.cpp\n+++ /dev/null\n@@ -1,4 +0,0 @@\n"
    assert [(r.path, r.start, r.end) for r in fb.pre_image_ranges(diff)] == [("src/Old.cpp", 1, 4)]


def test_ranges_are_capped_and_cut() -> None:
    hunks = "".join(f"@@ -{i * 100},{80} +1 @@\n" for i in range(1, 20))
    got = fb.pre_image_ranges("--- a/src/A.cpp\n+++ b/src/A.cpp\n" + hunks)
    assert len(got) == fb.MAX_RANGES and all(r.lines == fb.MAX_LINES_PER_RANGE for r in got)


def test_tally_splits_a_range_by_blamed_lines_and_never_counts_the_fix() -> None:
    r1 = fb.Range("src/A.cpp", 10, 19)  # 10 lines
    r2 = fb.Range("src/B.cpp", 5, 5, weight=0.5)
    votes = fb.tally(
        [
            (
                r1,
                [
                    {"pr_number": 100730, "lines_blamed": 8, "title": "culprit"},
                    {"pr_number": 83777, "lines_blamed": 2},
                ],
            ),
            (
                r2,
                [
                    {"pr_number": 107997, "lines_blamed": 1},
                    {"pr_number": 100730, "lines_blamed": 1},
                ],
            ),
        ],
        exclude={107997},
    )
    assert [(v.pr_number, round(v.lines, 2), v.ranges) for v in votes] == [
        (100730, 8.5, 2),
        (83777, 2.0, 1),
    ]
    assert votes[0].title == "culprit" and votes[0].paths == ["src/A.cpp", "src/B.cpp"]


def test_promote_puts_blamed_prs_first_and_adds_unreached_ones() -> None:
    pool = [
        bi.Candidate(
            pr_number=1, title="a", author="", merged_at="2026-01-01", minor_version="26.1"
        ),
        bi.Candidate(
            pr_number=2, title="b", author="", merged_at="2026-01-02", minor_version="26.1"
        ),
    ]
    votes = [
        fb.Vote(pr_number=9, lines=6.0, ranges=2, paths=["src/A.cpp"], title="culprit"),
        fb.Vote(pr_number=2, lines=1.0, ranges=1),
    ]
    out = bi.promote_fix_blame(pool, votes, fix_pr=77)
    assert [c.pr_number for c in out] == [9, 2, 1]
    d = out[0].as_dict()
    assert d["blamed_by_fix"] == {"fix_pr": 77, "lines": 6.0, "ranges": 2, "paths": ["src/A.cpp"]}
    assert "blamed_by_fix" not in out[2].as_dict()


def test_the_fix_blame_stage_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app import bisect_fix

    class Repo:
        async def merge_commit_of(self, org: str, ns: str, pr: int) -> str:
            raise RuntimeError("ledger down")

    pool = [bi.Candidate(pr_number=1, title="a", author="", merged_at="", minor_version="")]
    out, summary = asyncio.run(bisect_fix._fix_blame_stage(Repo(), "o", "n", pool, 5))  # type: ignore[arg-type]
    assert out == pool and summary == {"fix_pr": 5, "skipped": "unavailable"}


def test_a_fix_missing_from_the_ledger_says_so() -> None:
    from server.code.app import bisect_fix

    class Repo:
        async def merge_commit_of(self, org: str, ns: str, pr: int) -> str:
            return ""

    out, summary = asyncio.run(bisect_fix._fix_blame_stage(Repo(), "o", "n", [], 5))  # type: ignore[arg-type]
    assert out == [] and summary["skipped"] == "fix_not_in_ledger"


def test_bisect_accepts_a_fix_pr_alone() -> None:
    from server.code.api.models_history import CodeBisectCandidatesInput

    assert CodeBisectCandidatesInput(org="o", namespace="n", fix_pr=107997).fix_pr == 107997
