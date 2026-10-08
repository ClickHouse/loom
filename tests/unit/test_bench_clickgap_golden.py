"""bench/clickgap_golden_replay.py: the fixture set's
integrity and the pure scoring rules the replay applies to loom's answers."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from bench.clickgap_golden_replay import (
    FIXTURES_DIR,
    clickgap_recall_query,
    coverage_measurable,
    coverage_subjects,
    expected_gate,
    load_fixtures,
    locate_touched,
    location_exists,
    locations_of,
    markdown_table,
    render_draft,
    score_g1,
    summarize,
    surfaced_rank,
)

# ------------------------------------------------------------------ fixtures


@pytest.mark.skipif(not FIXTURES_DIR, reason="LOOM_CLICKGAP_GOLDEN_DIR not set (private data)")
def test_fixture_set_is_the_thirty_clickgap_captured() -> None:
    rows = load_fixtures()
    assert len(rows) == 30
    assert Counter(r["outcome_category"] for r in rows) == {
        "PUSHBACK": 9,
        "DROPPED_CORRECT": 8,
        "ISSUE_CREATED": 5,
        "MERGED": 5,
        "DROPPED_GATE": 2,
        "EDGE_CASE": 1,
    }
    assert sum(1 for r in rows if r["is_pushback_seed"]) == 9
    for r in rows:
        assert r["fixture_id"].startswith("h_pr")
        assert len(r["captured_sha"]) == 40
        assert expected_gate(r, "G1_SOURCE_EXISTS") in (True, False)
        assert expected_gate(r, "G4_TEST_SUITE_SEARCHED") is True
        assert locations_of(json.loads(r["finding"]["hypothesis_json"]))


def test_locations_of_reads_both_shapes_and_dedups() -> None:
    hyp = {
        "source_locations": ["src/A.cpp:10", "src/B.h"],
        "affected_locations": [
            {"file": "src/A.cpp", "line": 10},
            {"file": "src/C.cpp", "line": 7},
            "src/D.cpp:3",
        ],
    }
    assert locations_of(hyp) == [
        ("src/A.cpp", 10),
        ("src/B.h", 0),
        ("src/C.cpp", 7),
        ("src/D.cpp", 3),
    ]


def test_render_draft_carries_every_citation_shape() -> None:
    fixture = {
        "finding": {
            "title": "T",
            "hypothesis_json": json.dumps(
                {
                    "what": "w",
                    "affected_locations": [{"file": "src/X.cpp", "line": 5}],
                    "callers_found": ["src/Y.cpp:9 - Foo::bar() calls it"],
                }
            ),
        }
    }
    text = render_draft(fixture)
    assert text.startswith("T\nwhat: w")
    assert "locations: src/X.cpp:5" in text
    assert "callers_found: src/Y.cpp:9 - Foo::bar() calls it" in text


# ------------------------------------------------------------------ G1


def _cit(verdict: str, reason: str = "", at: dict | None = None, line: int = 5) -> dict:
    return {
        "kind": "location",
        "text": f"src/X.cpp:{line}",
        "path": "src/X.cpp",
        "start_line": line,
        "verdict": verdict,
        "reason": reason,
        "target": {"at_commit": at} if at else {},
    }


def test_location_exists_is_the_bots_g1_rule_at_a_commit() -> None:
    assert location_exists(_cit("resolved", at={"existed": True, "lines": 100}), True) is True
    assert (
        location_exists(
            _cit("stale", "line_beyond_eof_at_commit", at={"existed": True, "lines": 3}), True
        )
        is False
    )
    assert (
        location_exists(_cit("stale", "path_absent_at_commit", at={"existed": False}), True)
        is False
    )
    # the mirror could not check → fall through to the head rule
    assert location_exists(_cit("resolved", at={"checked": False}), True) is True


def test_location_exists_at_head_counts_unit_gaps_as_existing() -> None:
    assert location_exists(_cit("resolved"), False) is True
    assert location_exists(_cit("stale", "line_outside_units"), False) is True
    assert location_exists(_cit("stale", "file_changed_since_commit"), False) is True
    assert location_exists(_cit("stale", "line_beyond_eof"), False) is False
    assert location_exists(_cit("unresolved", "path_not_indexed"), False) is None
    assert location_exists(_cit("unresolved", "not_covered"), False) is None
    assert location_exists(_cit("ambiguous", "several_files"), False) is None


def test_score_g1_any_existing_location_passes_and_undecided_is_none() -> None:
    payload = {
        "verdict": "has_stale",
        "counts": {"total": 2},
        "citations": [_cit("stale", "line_beyond_eof", line=900), _cit("resolved", line=5)],
        "at_commit": {"sha": "abc", "calls": 2},
    }
    scored = score_g1(payload, [("src/X.cpp", 900), ("src/X.cpp", 5)], anchored=False)
    assert scored["loom_g1"] is True
    assert [p["exists"] for p in scored["locations"]] == [False, True]
    assert scored["commit_known"] is True
    unknown = score_g1(
        {"citations": [_cit("unresolved", "path_not_indexed")]}, [("src/X.cpp", 5)], False
    )
    assert unknown["loom_g1"] is None and unknown["commit_known"] is False


# ------------------------------------------------------------------ impact / coverage


def test_locate_touched_picks_the_nearest_unit_starting_at_or_before_the_line() -> None:
    payload = {
        "touched_symbols": [
            {"qualified_name": "A::ctor", "path": "src/X.cpp", "start_line": 10},
            {"qualified_name": "A::run", "path": "src/X.cpp", "start_line": 40},
            {"qualified_name": "B::b", "path": "src/Y.cpp", "start_line": 1},
        ],
        "verdict": "",
        "by_hop_counts": {"1": 4, "2": 9},
        "impacted_tests": [{}, {}],
        "paths_seen": [{"class": "src"}, {"class": "test"}],
    }
    out = locate_touched(payload, [("src/X.cpp", 45), ("src/X.cpp", 3), ("src/Z.cpp", 1)])
    assert [r["unit"] for r in out["rows"]] == ["A::run", None, None]
    assert out["located"] == 1 and out["n"] == 3 and out["impacted_tests"] == 2
    assert out["impacted"] == 13 and out["verdict"] == ""  # callers found: no empty-radius class
    assert out["paths_seen"] == {"src": 1, "test": 1}


def test_coverage_subjects_prefer_resolved_names_then_the_first_src_path() -> None:
    vc = {
        "citations": [
            {"kind": "name", "text": "std::move", "verdict": "unresolved"},
            {"kind": "name", "text": "DB::Foo::bar", "verdict": "resolved"},
            {"kind": "name", "text": "DB::Foo::bar", "verdict": "resolved"},
            {"kind": "name", "text": "DB::Baz", "verdict": "ambiguous"},
        ]
    }
    assert coverage_subjects(vc, [("src/X.cpp", 1)]) == [
        {"symbol": "DB::Foo::bar"},
        {"symbol": "DB::Baz"},
    ]
    locs = [("tests/queries/0_stateless/1.sql", 0), ("src/X.cpp", 3)]
    assert coverage_subjects({"citations": []}, locs) == [{"path": "src/X.cpp"}]
    assert coverage_subjects({}, []) == []


def test_coverage_measurable_needs_a_passing_control_and_a_stated_verdict() -> None:
    assert coverage_measurable({"control": {"ok": True}, "verdict": "uncovered"}) is True
    assert coverage_measurable({"control": {"ok": True}, "verdict": "covered_by"}) is True
    assert coverage_measurable({"control": {"ok": True}, "verdict": "unknown"}) is False
    assert coverage_measurable({"control": {"ok": False}, "verdict": "uncovered"}) is False


# ------------------------------------------------------------------ negative gate


def test_clickgap_recall_query_matches_the_bots_shape() -> None:
    q = clickgap_recall_query(["src/Storages/A_b.cpp"] * 12, "Add null_count", "## Changelog - x")
    assert q.startswith("src Storages A b cpp " * 10)
    assert "Add null_count" in q and q.endswith(" Changelog   x")
    assert clickgap_recall_query([], "") == ""


def test_surfaced_rank_is_one_based_or_none() -> None:
    payload = {"results": [{"memory_key": "a"}, {"memory_key": "finding_x"}]}
    assert surfaced_rank(payload, "finding_x") == 2
    assert surfaced_rank(payload, "finding_y") is None
    assert surfaced_rank({}, "finding_x") is None


# ------------------------------------------------------------------ summary


def _row(fid: str, exp_g1: bool, loom_g1: bool | None, *, negative: dict | None = None) -> dict:
    return {
        "fixture_id": fid,
        "pr": 1,
        "outcome_category": "PUSHBACK" if negative else "MERGED",
        "expected_g1": exp_g1,
        "expected_g4": True,
        "g1": {
            "captured": {"loom_g1": loom_g1, "commit_known": True, "sha": "abc"},
            "merge": {"loom_g1": loom_g1, "commit_known": False, "sha": ""},
        },
        "impact": {"located": 1, "n": 2, "verdict": "no_static_callers", "rows": []},
        "coverage": [{"verdict": "uncovered", "measurable": True}],
        **({"negative": negative} if negative else {}),
    }


def test_summarize_counts_agreement_undecided_and_the_negative_gate() -> None:
    rows = [
        _row("a", True, True),
        _row("b", False, True),
        _row("c", True, None),
        _row("d", True, True, negative={"rank": None, "control_rank": 1}),
        _row("e", True, True, negative={"rank": 3, "control_rank": 1}),
    ]
    s = summarize(rows)
    assert s["g1_captured"] == {"agree": 3, "decided": 4, "undecided": 1, "commit_known": 5}
    assert s["g1_merge"]["commit_known"] == 0
    assert s["impact"]["located"] == 5 and s["impact"]["locations"] == 10
    assert s["coverage"] == {
        "subjects": 5,
        "errored": 0,
        "measurable": 5,
        "verdicts": Counter({"uncovered": 5}),
        "g4_agree": 5,
        "g4_decided": 5,
    }
    assert s["negative"] == {
        "fixtures": 2,
        "errored": 0,
        "not_surfaced": 1,
        "control_surfaced": 2,
        "not_surfaced_and_control_surfaced": 1,
    }
    table = markdown_table(rows)
    assert table.count("\n") == 6  # header, rule, five rows
    assert (
        "| d | 1 | PUSHBACK | T | T | T | 1/2 | no_static_callers | uncovered | T / T | - / 1 |"
        in table
    )


def test_summarize_counts_failed_calls_as_errors_not_passes() -> None:
    """A failed memory.search surfaces nothing and a failed coverage_claim is not
    measurable; neither may read as a negative-gate pass or a G4 disagreement."""
    err_neg = {"rank": None, "control_rank": None, "error": "503"}
    ok = _row("a", True, True, negative={"rank": None, "control_rank": 1})
    bad = _row("b", True, True, negative=err_neg)
    bad["coverage"] = [{"verdict": None, "measurable": False, "error": "503"}]
    s = summarize([ok, bad])
    assert s["negative"]["fixtures"] == 1 and s["negative"]["errored"] == 1
    assert s["negative"]["not_surfaced"] == 1
    assert s["coverage"]["errored"] == 1
    assert s["coverage"]["g4_decided"] == 1 and s["coverage"]["g4_agree"] == 1
    bad["expected_g1"] = None
    assert "| b | 1 | PUSHBACK | ? |" in markdown_table([bad])


@pytest.mark.parametrize("gate", ["G1_SOURCE_EXISTS", "G9_MISSING"])
def test_expected_gate_is_none_when_the_fixture_lacks_it(gate: str) -> None:
    assert (
        expected_gate(
            {"expected": {"gates": {"results_by_gate": {"G1_SOURCE_EXISTS": "yes"}}}}, gate
        )
        is None
    )
