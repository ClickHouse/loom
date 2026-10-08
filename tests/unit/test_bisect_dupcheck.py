"""A nearest issue sharing the defect's words stays on topic
even when the draft quotes an error code the title does not repeat."""

from __future__ import annotations

from datetime import datetime

import server.tracker.domain.bisect as bi
from server.tracker.domain import dupcheck_closest as dcl


def test_a_draft_quoting_an_error_code_keeps_a_title_that_shares_the_defect() -> None:
    draft = (
        "ALTER TABLE DROP COLUMN fails Unknown expression or function identifier l1 "
        "UNKNOWN_IDENTIFIER with ALIAS column calling SQL UDF with alias"
    )
    assert dcl.on_topic(draft, "Cannot DROP COLUMN if there's a complex ALIAS in table")
    # one shared word is not enough when the draft names an identifier
    assert not dcl.on_topic(draft, "Wrong result for an ALIAS in a view")
    # the identifier alone still admits a title
    assert dcl.on_topic(draft, "UNKNOWN_IDENTIFIER after upgrade")


def test_the_owners_error_code_draft_still_drops_off_topic_titles() -> None:
    draft = "IllegalTypeOfArgument in toStartOfInterval"
    assert dcl.on_topic(draft, "toStartOfInterval WEEK bucket for Date saturates to 1970-01-01")
    assert not dcl.on_topic(draft, "WITH FILL usage")


def _cand(n: int, merged: str, minor: str = "26.6") -> bi.Candidate:
    return bi.Candidate(
        pr_number=n,
        title=f"Change {n}",
        author="a",
        merged_at=merged,
        minor_version=minor,
        matched_paths=["src/Storages/MergeTree/MergeTreeIndexSet.cpp"],
    )


def test_a_relevant_fix_naming_a_candidate_puts_it_first() -> None:
    # a real case: the fix #107868 (same file) says "Caused by #105384"; the culprit ranked 3rd
    pool = [
        _cand(107868, "2026-06-20T00:00:00"),
        _cand(104890, "2026-05-25T00:00:00"),
        _cand(105384, "2026-05-20T05:37:43", "26.5"),
    ]
    stmts = [
        {
            "pr_number": 107868,
            "title": "Fix `set` skip index not pruning granules over `LowCardinality` columns",
            "merged_at": datetime(2026, 6, 22),
            "files": ["src/Storages/MergeTree/MergeTreeIndexSet.cpp"],
            "names": [105384],
        }
    ]
    out = bi.promote_named_causes(
        pool,
        stmts,
        paths=["src/Storages/MergeTree/MergeTreeIndexSet.cpp"],
        frames=["MergeTreeIndexSet"],
        patterns=[],
    )
    assert [c.pr_number for c in out] == [105384, 107868, 104890]
    assert out[0].as_dict()["named_cause_by"] == [{"pr_number": 107868, "title": stmts[0]["title"]}]
    assert "named_cause_by" not in out[1].as_dict()


def test_a_statement_about_another_bug_is_ignored() -> None:
    # a real case: #107071 "Support trim character sets…" names #93543 for a trim bug
    pool = [_cand(100699, "2026-02-01T00:00:00"), _cand(93543, "2026-01-10T00:00:00")]
    stmts = [
        {
            "pr_number": 107071,
            "title": "Support trim character sets longer than 16 in trim* functions",
            "merged_at": datetime(2026, 6, 1),
            "files": ["src/Functions/trim.cpp"],
            "names": [93543],
        }
    ]
    out = bi.promote_named_causes(
        pool,
        stmts,
        paths=["src/Functions/if.cpp"],
        frames=["FunctionIf"],
        patterns=["Decimal", "scale"],
    )
    assert [c.pr_number for c in out] == [100699, 93543] and not out[1].named_cause_by


def test_a_title_sharing_a_pattern_is_relevant_and_an_earlier_statement_is_not() -> None:
    # a real case: #103809 "Fix wrong values from JIT-compiled if/multiIf with Decimal result"
    stmt = {
        "pr_number": 103809,
        "title": "Fix wrong values from JIT-compiled if/multiIf with Decimal result",
        "merged_at": datetime(2026, 5, 6),
        "files": ["src/DataTypes/Native.cpp"],
        "names": [88770],
    }
    assert bi.relevant_statement(
        stmt, paths=["src/Functions/if.cpp"], frames=[], patterns=["Decimal"]
    )
    # a real case: the frame `readBigAt` in a title written as "readbigat"
    assert bi.relevant_statement(
        {"title": "Fix async bounded read buffer readbigat race", "files": []},
        paths=[],
        frames=["readBigAt"],
        patterns=[],
    )
    # a statement cannot predate the change it blames
    pool = [_cand(88770, "2026-06-01T00:00:00")]
    assert not bi.promote_named_causes(pool, [stmt], paths=[], frames=[], patterns=["Decimal"])[
        0
    ].named_cause_by


def test_the_mcp_duplicate_check_answer_keeps_the_verdict_and_drops_the_fit() -> None:
    from server.transport.lean_results import lean_dupcheck

    raw = {
        "verdict": "none",
        "epistemic": "calibrated",
        "items": [
            {"number": 1, "features": {"x": 1.0}, "confidence": 0.4, "relation_recorded": "none"}
        ],
        "nearest": [{"number": 2}],
        "needs": [],
        "fit": {"auc": 0.99},
        "fit_at": "t",
        "thresholds": {"duplicate": 0.36},
        "query": {"timings_ms": {}},
        "legs": {"lexical": "served"},
        "by_namespace": {"a": {}},
        "namespaces": ["qa"],
    }
    out = lean_dupcheck(raw)
    assert set(out) == {"verdict", "epistemic", "items", "nearest", "needs", "namespaces"}
    assert out["items"] == [{"number": 1, "confidence": 0.4}]
