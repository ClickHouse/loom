"""`code.tests_for` — the tests whose statement records carry
every named facet, one keyed read of loom.code_test_facets."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app import coverage as cc
from server.code.store.index.facets import _fold_facet_hits


def test_fold_reads_per_facet_ordinals_and_co_occurrence() -> None:
    pairs = [("error_code", "BAD_ARGUMENTS"), ("function", "arrayJoin")]
    row = {
        "path": "tests/queries/0_stateless/1.sql",
        "hits": [
            ("function", "arrayJoin", 3, 12, 0.8),
            ("function", "arrayJoin", 7, 30, 0.8),
            ("error_code", "BAD_ARGUMENTS", 7, 30, 1.0),
        ],
    }
    out = _fold_facet_hits(row, pairs)
    assert out == {
        "path": "tests/queries/0_stateless/1.sql",
        "ordinals": [3, 7],
        "lines": [12, 30],
        "confidence": 0.8,
        "same_statement": True,
    }
    row["hits"] = [
        ("function", "arrayJoin", 3, 12, 0.8),
        ("error_code", "BAD_ARGUMENTS", 7, 30, 1.0),
    ]
    assert _fold_facet_hits(row, pairs)["same_statement"] is False


def test_facet_pairs_validate_kind_and_name() -> None:
    from server.core.errors import ValidationError

    assert cc._facet_pairs(
        [{"kind": "setting", "name": " dialect "}, {"kind": "setting", "name": "dialect"}]
    ) == [("setting", "dialect")]
    with pytest.raises(ValidationError):
        cc._facet_pairs([{"kind": "table", "name": "t"}])
    with pytest.raises(ValidationError):
        cc._facet_pairs([{"kind": "setting", "name": ""}])


class _Repo:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        total: int,
        present: bool = True,
        kinds: set[str] | None = None,
    ) -> None:
        self.rows, self.total, self.present = rows, total, present
        # The kinds the namespace holds rows for; by default
        # every asked kind when `present`, none otherwise
        self.kinds = kinds
        self.calls: list[Any] = []

    async def facet_kinds_present(self, org: str, ns: str, kinds: list[str]) -> set[str]:
        if self.kinds is not None:
            return self.kinds & set(kinds)
        return set(kinds) if self.present else set()

    async def tests_for_facets(self, org: str, ns: str, facets, *, limit: int = 50):
        self.calls.append((org, ns, list(facets), limit))
        return self.rows[:limit], self.total

    async def facets_present(self, org: str, ns: str) -> bool:
        return self.present


class _Input:
    org = "clickhouse"
    namespace = "code-clickhouse"
    agent = "-"
    user_id = "-"
    consumer = "test"
    facets = [{"kind": "setting", "name": "dialect"}]
    limit = 2


def _patch(monkeypatch, repo: _Repo) -> None:
    monkeypatch.setattr(cc, "CodeRepo", lambda: repo)
    monkeypatch.setattr(cc, "require_code_intel", lambda: None)
    monkeypatch.setattr(
        cc, "resolve_code_identity", lambda o, n: (o or "clickhouse", n or "code-clickhouse")
    )
    monkeypatch.setattr(cc, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(cc, "set_code_span_attributes", lambda **kw: None)

    async def _as_of(repo, org, ns):
        return {"commit": "abc"}

    import server.code.app.common as common

    monkeypatch.setattr(common, "index_as_of", _as_of)


def test_handler_returns_rows_total_and_truncation(monkeypatch) -> None:
    rows = [
        {
            "path": "t/1.sql",
            "ordinals": [4],
            "lines": [9],
            "confidence": 1.0,
            "same_statement": True,
        },
        {
            "path": "t/2.sql",
            "ordinals": [1],
            "lines": [3],
            "confidence": 1.0,
            "same_statement": True,
        },
    ]
    repo = _Repo(rows, total=5)
    _patch(monkeypatch, repo)
    out = asyncio.run(cc.handle_code_tests_for(_Input()))
    assert [t["path"] for t in out.tests] == ["t/1.sql", "t/2.sql"]
    assert out.total == 5 and out.truncated is True
    assert out.verdict == "" and out.abstain is False
    assert out.facets == [{"kind": "setting", "name": "dialect"}]
    assert repo.calls == [("clickhouse", "code-clickhouse", [("setting", "dialect")], 2)]


def test_empty_answer_names_why(monkeypatch) -> None:
    _patch(monkeypatch, _Repo([], total=0, present=True))
    out = asyncio.run(cc.handle_code_tests_for(_Input()))
    assert out.verdict == "no_match" and out.abstain is False and out.tests == []
    _patch(monkeypatch, _Repo([], total=0, present=False))
    out = asyncio.run(cc.handle_code_tests_for(_Input()))
    assert out.verdict == "not_covered" and out.abstain is True
    assert out.uncovered_kinds == ["setting"]


def test_a_kind_no_lane_wrote_is_not_covered_even_when_other_kinds_are(monkeypatch) -> None:
    """A TypeScript tree holds table_read rows (its SQL
    islands) and no `setting` rows; asking it for a setting must abstain so
    the caller greps, not answer "no test sets it"."""

    class _In(_Input):
        facets = [{"kind": "table_read", "name": "traces"}, {"kind": "setting", "name": "dialect"}]

    _patch(monkeypatch, _Repo([], total=0, kinds={"table_read", "dialect"}))
    out = asyncio.run(cc.handle_code_tests_for(_In()))
    assert out.verdict == "not_covered" and out.abstain is True
    assert out.uncovered_kinds == ["setting"]
    # every asked kind present, none carrying the name: an answer
    _patch(monkeypatch, _Repo([], total=0, kinds={"table_read", "setting"}))
    out = asyncio.run(cc.handle_code_tests_for(_In()))
    assert out.verdict == "no_match" and out.abstain is False and out.uncovered_kinds == []


def test_operation_is_registered_as_a_viewer_read() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["code.tests_for"]
    assert op.idempotent is True
    assert op.input_model.model_fields["facets"].metadata  # min/max length constraints


def test_every_lane_facet_kind_is_accepted() -> None:
    """Write facet kinds beyond the nine statement-record kinds (e.g.
    `table_write`); code.tests_for must accept them."""
    from server.code.app.coverage import _facet_pairs

    kinds = [
        "dialect",
        "program",
        "tool",
        "migration",
        "direction",
        "creates",
        "table_read",
        "table_write",
        "source",
        "materialized",
        "macro_call",
    ]
    pairs = _facet_pairs([{"kind": k, "name": "x"} for k in kinds])
    assert [k for k, _ in pairs] == kinds
    with pytest.raises(Exception, match="unknown facet kind"):
        _facet_pairs([{"kind": "colour", "name": "x"}])
