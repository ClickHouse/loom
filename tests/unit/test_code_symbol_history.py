"""code.symbol_history: the timeline is loom's file-level PR
record narrowed by QA's diff text to the function, with linked issues,
reverts, review comments inside the span, what the PRs caused and the open
issues naming the function; every count a floor; QA absent = file-level
and said so."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.code.app.symbol as cs
import server.code.app.symbol_history as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeSymbolHistoryInput
from server.code.domain.index import symbol_history as sh
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
REPO = "ClickHouse/ClickHouse"
PATH = "src/Storages/MergeTree/MergeTreeData.cpp"
SEED = {
    "qualified_name": "DB::MergeTreeData::loadDataParts",
    "path": PATH,
    "start_line": 1200,
    "end_line": 1400,
    "tier": "scip",
    "symbol_id": "s1",
}
PRS = [
    {
        "pr_number": 900,
        "title": "Fix crash in parts loading",
        "author": "a",
        "merged_at": datetime(2026, 9, 1),
        "linked_issues": [100],
        "is_revert": False,
        "files": [PATH],
    },
    {
        "pr_number": 850,
        "title": 'Revert "Parallel parts loading"',
        "author": "b",
        "merged_at": datetime(2026, 8, 1),
        "linked_issues": [],
        "is_revert": True,
        "files": [PATH],
    },
    {
        "pr_number": 800,
        "title": "Rename a variable",
        "author": "robot-ch",
        "merged_at": datetime(2026, 7, 1),
        "linked_issues": [],
        "is_revert": False,
        "files": [PATH],
    },
    {
        "pr_number": 700,
        "title": "Parallel parts loading",
        "author": "c",
        "merged_at": datetime(2026, 6, 1),
        "linked_issues": [],
        "is_revert": False,
        "files": [PATH],
        "body_excerpt": "Makes loadDataParts run in a thread pool.",
    },
]
COMMENTS = [
    {
        "pr_number": 900,
        "line": 1250,
        "author": "rev",
        "body": "x" * 60,
        "comment_created_at": datetime(2026, 9, 1),
    },
    {
        "pr_number": 700,
        "line": 20,
        "author": "rev",
        "body": "y" * 60,
        "comment_created_at": datetime(2026, 6, 1),
    },
]


# ---- pure helpers -------------------------------------------------------------


def test_in_span_and_fix_title() -> None:
    assert (
        sh.in_span(1250, 1200, 1400)
        and sh.in_span(1198, 1200, 1400)
        and not sh.in_span(20, 1200, 1400)
    )
    assert not sh.in_span(0, 1200, 1400)
    assert sh.is_fix_title("Fix crash in parts loading", []) and sh.is_fix_title("Refactor", [100])
    assert not sh.is_fix_title("Refactor", [])


def test_build_rows_marks_touch_from_tracker_and_summarises() -> None:
    tracker_facts = {
        900: {
            "repo": REPO,
            "matched_symbols": ["loadDataParts"],
            "has_test": 1,
            "ci_result": "success",
            "minor_version": "26.9",
        },
        800: {"repo": REPO, "matched_symbols": [], "has_test": None},
        700: {"repo": REPO, "matched_symbols": ["loadDataParts"], "has_test": 0},
    }
    edges = [
        {
            "src_ref": f"{REPO}#700",
            "rel_type": "caused_by",
            "dst_ref": f"{REPO}#100",
            "direction": "in",
        },
        {
            "src_ref": f"{REPO}#900",
            "rel_type": "fixes",
            "dst_ref": f"{REPO}#101",
            "direction": "out",
        },
    ]
    rows = sh.build_rows(
        PRS,
        issue_facts={100: {"title": "Crash on restart", "state": "closed"}},
        comments=COMMENTS,
        span=(1200, 1400),
        tracker_facts=tracker_facts,
        tracker_edges=edges,
        frames=["loadDataParts"],
        qa_ok=True,
    )
    by = {r.pr_number: r for r in rows}
    assert [r.pr_number for r in rows] == [900, 850, 800, 700]
    assert (
        by[900].touches_symbol == "true"
        and by[900].tracker["has_test"] is True
        and by[900].tracker["first_release"] == "26.9"
    )
    assert by[900].comments_on_symbol == 1 and by[900].is_fix
    assert [i["number"] for i in by[900].linked_issues] == [100, 101] and by[900].linked_issues[0][
        "title"
    ] == "Crash on restart"
    assert (
        by[800].touches_symbol == "false" and by[850].touches_symbol == "unknown"
    )  # no QA row for 850
    assert by[900].comments_on_symbol == 1
    assert by[700].tracker["caused"] == [100] and by[700].touches_symbol == "true"
    summary = sh.summarize(rows, window_prs=60, open_issues=2, qa_ok=True)
    assert summary["prs_total"] == 3 and summary["fix_prs"] == 1 and summary["reverts"] == 1
    assert (
        summary["named_prs"] == 2 and summary["named_fix_prs"] == 1
    )  # 900 (QA + comment), 700 (QA + body)
    assert summary["caused_issues"] == [100] and summary["authors"] == ["a", "b", "c"]
    assert summary["note"].startswith(
        "3 PRs touched this function in the 60-PR window (2026-06-01 → 2026-09-01); "
        "2 name the function outright (1 fix); 1 were fixes; 1 revert"
    )
    assert "2 open issues name it today" in summary["note"]


def test_without_tracker_rows_stay_file_level_and_the_note_says_so() -> None:
    rows = sh.build_rows(
        PRS,
        issue_facts={},
        comments=[],
        span=(1200, 1400),
        tracker_facts={},
        tracker_edges=[],
        frames=["loadDataParts"],
        qa_ok=False,
    )
    by = {r.pr_number: r for r in rows}
    assert (
        by[700].touches_symbol == "named" and by[900].touches_symbol == "unknown"
    )  # body names it / nothing does
    summary = sh.summarize(rows, window_prs=60, open_issues=0, qa_ok=False)
    assert summary["prs_total"] == 4 and summary["named_prs"] == 1
    assert "its file" in summary["note"] and "tracker record unavailable" in summary["note"]
    assert sh.verdict_of([], resolved=False) == ("unknown", ["symbol_unresolved"])
    assert sh.verdict_of([], resolved=True) == ("no_history", [])


# ---- handler -----------------------------------------------------------------


class _Repo:
    async def pr_history_for(self, org, ns, *, paths=None, symbol_names=None, limit=5):
        assert paths == [PATH]
        return list(PRS)

    async def review_comments_for(
        self, org, ns, paths, *, exclude_pr=0, limit=30, humans_only=True
    ):
        return list(COMMENTS)

    async def items_by_number(self, org, ns, numbers):
        return {100: {"title": "Crash on restart", "state": "closed", "is_pr": 0}}


class _Source:
    def __init__(self, *, down=False) -> None:
        self.down = down

    async def prs_facts(self, org, ns, numbers, *, names=None):
        if self.down:
            raise TrackerUnavailable("OPEN")
        return {
            900: {
                "repo": REPO,
                "matched_symbols": list(names or []),
                "has_test": 1,
                "ci_result": "success",
                "minor_version": "26.9",
            }
        }

    async def open_issues_naming(self, org, ns, tokens, *, limit=5):
        return [
            {
                "number": 300,
                "title": "loadDataParts slow",
                "severity_level": "medium",
                "primary_component": "MergeTree",
            }
        ]

    async def edges_from(self, org, ns, refs, rels=None):
        return []


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(h, "CodeRepo", lambda: _Repo())
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    state: dict[str, Any] = {"seeds": [SEED], "src": _Source(), "qa": QA}

    async def _resolve(repo, org, ns, *, symbol_id, name):
        return list(state["seeds"])

    monkeypatch.setattr(cs, "_resolve_symbols", _resolve)

    async def _authorized(org, code_ns):
        return state["qa"]

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: state["src"])
    return state


def test_op_is_registered_experimental_viewer_read() -> None:
    op = OPERATIONS_BY_NAME["code.symbol_history"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    # the "name or symbol_id" rule is not the model's
    assert CodeSymbolHistoryInput(org=ORG, namespace=NS).name == ""


@pytest.mark.asyncio
async def test_missing_subject_is_refused_in_the_handler_after_the_gate(wired) -> None:
    """The "name or symbol_id" rule lives in the handler, after the namespace
    gate (as the review brief, backport gap and first build ops do), so a refused principal
    never learns the op's argument shape from a model-level 422."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="name or symbol_id is required"):
        await h.handle_code_symbol_history(CodeSymbolHistoryInput(org=ORG, namespace=NS))
    with pytest.raises(ValidationError):
        await h.handle_code_symbol_history(CodeSymbolHistoryInput(org=ORG, namespace=NS, name="  "))


@pytest.mark.asyncio
async def test_handler_assembles_loom_and_tracker(wired) -> None:
    out = await h.handle_code_symbol_history(
        CodeSymbolHistoryInput(
            org=ORG, namespace=NS, name="DB::MergeTreeData::loadDataParts", limit=3
        )
    )
    assert (
        out.verdict == "history"
        and out.causes == []
        and out.tracker_namespace == QA
        and not out.tracker_unavailable
    )
    assert out.symbol["path"] == PATH and out.symbol["start_line"] == 1200
    assert [p["pr_number"] for p in out.prs] == [900, 850, 800]
    assert out.prs[0]["touches_symbol"] == "true" and out.prs[0]["qa"]["has_test"] is True
    assert out.prs[0]["linked_issues"][0] == {
        "number": 100,
        "title": "Crash on restart",
        "state": "closed",
    }
    assert out.review_comments == [
        {
            "pr_number": 900,
            "line": 1250,
            "author": "rev",
            "excerpt": "x" * 60,
            "at": "2026-09-01T00:00:00",
        }
    ]
    assert out.open_issues_naming[0]["number"] == 300 and out.summary["open_issues_naming"] == 1
    assert out.summary["reverts"] == 1 and out.summary["last_touched"] == "2026-09-01"


@pytest.mark.asyncio
async def test_unbound_unavailable_and_unresolved(wired) -> None:
    wired["qa"] = ""
    out = await h.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace=NS, name="x")
    )
    assert out.verdict == "history" and out.tracker_namespace == "" and not out.tracker_unavailable
    assert {p["touches_symbol"] for p in out.prs} == {
        "unknown",
        "named",
    } and out.open_issues_naming == []
    wired["qa"], wired["src"] = QA, _Source(down=True)
    out = await h.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace=NS, name="x")
    )
    assert (
        out.tracker_unavailable
        and out.causes == ["tracker_unavailable"]
        and out.summary["prs_total"] == 4
    )
    wired["seeds"] = []
    out = await h.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace=NS, name="nope")
    )
    assert out.verdict == "unknown" and out.causes == ["symbol_unresolved"] and out.prs == []


@pytest.mark.asyncio
async def test_a_name_with_several_definitions_answers_ambiguous(wired) -> None:
    """Picking the first definition would attach another function's PRs to the answer."""
    other = {**SEED, "qualified_name": "DB::Other::" + "loadDataParts", "path": "src/Other.cpp"}
    wired["seeds"] = [SEED, other]
    out = await h.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace=NS, name="loadDataParts")
    )
    assert out.ambiguous is True and out.verdict == "unknown"
    assert out.causes == ["symbol_ambiguous"] and out.prs == []
    assert out.candidates == [SEED["qualified_name"], other["qualified_name"]]


@pytest.mark.asyncio
async def test_two_rows_of_one_definition_are_not_ambiguous(wired) -> None:
    wired["seeds"] = [SEED, {**SEED, "tier": "treesitter"}]
    out = await h.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace=NS, name="loadDataParts")
    )
    assert out.ambiguous is False
