"""Loom_search answers a defect-report question from the tracker record."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from server.memory.app.loom_search import tracker_leg as ql
from server.tracker.api.models import TrackerDuplicateCheckOutput
from server.transport import mcp_tools


async def _run(
    query: str, *, org: str, top_k: int = 5, timeout_s: float | None = None
) -> dict[str, Any] | None:
    plan = await ql.prepare_tracker_search(query, org=org, top_k=top_k, timeout_s=timeout_s)
    return await ql.tracker_leg(query, plan=plan, top_k=top_k)


DRAFT = (
    "Is this already reported? LOGICAL_ERROR in MergeTreeData::renameTempPartAndAdd: added part covers "
    "existing parts\nAfter ALTER TABLE UPDATE under heavy inserts the replica restarted. "
    "Frames: DB::MergeTreeData::renameTempPartAndAdd, DB::MergeTreeData::renameTempPartAndReplaceImpl"
)


def test_the_shape_needs_two_of_three_signals() -> None:
    shape = ql.defect_report_shape(DRAFT)
    assert shape is not None and set(shape["signals"]) == {"frames", "error", "intent"}
    assert shape["frames"][:2] == [
        "MergeTreeData::renameTempPartAndAdd",
        "DB::MergeTreeData::renameTempPartAndAdd",
    ]
    # The draft is the report, without the question about it.
    assert (
        shape["title"].startswith("LOGICAL_ERROR in MergeTreeData")
        and "ALTER TABLE" in shape["body"]
    )
    # a frame plus the asking intent, no error token
    assert ql.defect_report_shape(
        "has DB::Aggregator::executeImpl crashing on merge been reported before?"
    )
    # an error token plus the intent, no frame
    assert ql.defect_report_shape(
        "Code: 252 Too many parts on insert — is this a known issue in 26.8?"
    )
    # one signal is a code or a memory question, not a defect report
    assert ql.defect_report_shape("where is MergeTreeData::renameTempPartAndAdd defined?") is None
    assert ql.defect_report_shape("what is my coffee type?") is None
    assert ql.defect_report_shape("remember that I prefer neovim") is None
    assert ql.defect_report_shape("LOGICAL_ERROR") is None, "too short"


def _out(**kw: Any) -> TrackerDuplicateCheckOutput:
    base = dict(
        verdict="related",
        epistemic="calibrated",
        namespaces=["tracker-example-private", "tracker-clickhouse"],
        items=[
            {
                "namespace": "tracker-clickhouse",
                "kind": "issue",
                "number": 64905,
                "title": "part covers parts",
                "state": "closed",
                "url": "https://g/64905",
                "confidence": 0.31,
                "relation": "related",
                "why": ["frames", "lexical"],
                "features": {"x": 1},
                "evidence": ["e"],
                "score": 0.9,
            }
        ],
    )
    base.update(kw)
    return TrackerDuplicateCheckOutput(**base)


@pytest.mark.asyncio
async def test_the_duplicate_verdict_runs_only_for_a_scoped_defect_and_fails_open(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    monkeypatch.setattr("server.identity.app.request_scope.request_scope", lambda: WHOLE_NAMESPACE)
    monkeypatch.setattr(
        "server.tracker.app.dupcheck_fanout.readable_tracker_namespaces", AsyncMock(return_value=[])
    )
    from server.tracker.app.dupcheck import DuplicateCheckPlan

    async def prepare(inp):
        return DuplicateCheckPlan(
            inp, ("tracker-example-private", "tracker-clickhouse"), inp.namespace, False, False
        )

    monkeypatch.setattr("server.tracker.app.dupcheck.prepare_duplicate_check", prepare)
    calls: list[dict[str, Any]] = []

    async def _check(plan):
        calls.append(plan.input.model_dump())
        return _out()

    monkeypatch.setattr("server.tracker.app.dupcheck.execute_duplicate_check", _check)
    monkeypatch.setattr("server.code.domain.scope.current_code_scope", lambda: None)
    assert await _run(DRAFT, org="clickhouse") is None and calls == [], (
        "no checkout scope: memory answers alone"
    )
    monkeypatch.setattr(
        "server.code.domain.scope.current_code_scope",
        lambda: ("clickhouse", "code-example-private"),
    )
    assert await _run("what is my coffee type?", org="clickhouse") is None and calls == [], (
        "not a defect shape"
    )
    section = await _run(DRAFT, org="clickhouse", top_k=3)
    assert calls[0]["org"] == "clickhouse" and calls[0]["namespace"] == "code-example-private"
    assert calls[0]["frames"][0] == "MergeTreeData::renameTempPartAndAdd" and calls[0]["top_k"] == 3
    assert section == {
        "verdict": "related",
        "epistemic": "calibrated",
        "undecided": False,
        "namespaces": ["tracker-example-private", "tracker-clickhouse"],
        "signals": ["frames", "error", "intent"],
        "results": [
            {
                "namespace": "tracker-clickhouse",
                "kind": "issue",
                "number": 64905,
                "title": "part covers parts",
                "state": "closed",
                "url": "https://g/64905",
                "confidence": 0.31,
                "relation": "related",
                "why": ["frames", "lexical"],
            }
        ],
    }, "the lean section: verdict, records read, items a reader acts on — no fit bookkeeping"

    async def _down(plan):
        return _out(
            verdict="none",
            epistemic="unavailable",
            tracker_unavailable=True,
            items=[],
            namespaces=[],
        )

    monkeypatch.setattr("server.tracker.app.dupcheck.execute_duplicate_check", _down)
    assert await _run(DRAFT, org="clickhouse") is None, "an unavailable record is no section"

    async def _boom(plan):
        raise RuntimeError("gate closed")

    monkeypatch.setattr("server.tracker.app.dupcheck.execute_duplicate_check", _boom)
    assert await _run(DRAFT, org="clickhouse") is None, "an error is no section"

    async def _slow(plan):
        await asyncio.sleep(0.2)
        return _out()

    monkeypatch.setattr("server.tracker.app.dupcheck.execute_duplicate_check", _slow)
    assert await _run(DRAFT, org="clickhouse", timeout_s=0.01) is None, "a timeout is no section"
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_loom_search_tracker_timeout_s", 0.01)
    assert await _run(DRAFT, org="clickhouse") is None, (
        "the setting bounds a leg that names no timeout"
    )


def test_the_text_block_leads_with_the_verdict() -> None:
    sec = ql.lean_tracker_section(_out(), {"signals": ["frames", "intent"]})
    lines = ql.tracker_text_lines(sec)
    assert lines[0] == (
        "tracker record (tracker-example-private + tracker-clickhouse): no likely duplicate in the indexed records (verdict related); "
        "closest reported items (1):"
    )
    assert (
        lines[1]
        == "  - [tracker-clickhouse] #64905 part covers parts (closed, 0.31) https://g/64905"
        and lines[-1] == ""
    )
    dup = ql.tracker_text_lines(ql.lean_tracker_section(_out(verdict="duplicate_of"), {}))
    assert dup[0].startswith(
        "tracker record (tracker-example-private + tracker-clickhouse): this looks ALREADY REPORTED"
    )
    none = ql.tracker_text_lines(ql.lean_tracker_section(_out(verdict="none", items=[]), {}))
    assert none == [
        "tracker record (tracker-example-private + tracker-clickhouse): no likely duplicate in the indexed records "
        "(verdict none, calibrated).",
        "",
    ]
    assert ql.tracker_text_lines(None) == []


def test_the_handler_gathers_the_leg_and_leads_the_payload_with_it() -> None:
    from server.memory.app.loom_search import fanout

    fan = inspect.getsource(fanout.search_sections)
    assert "tracker_leg(query, plan=tracker_plan, top_k=top_k)" in fan, (
        "the fifth leg rides the same gather"
    )
    assert '{"tracker": t_section} if isinstance(t_section, dict) else {}' in fan
    src = inspect.getsource(mcp_tools.search_answer)
    assert 'lines = tracker_text_lines(payload.get("tracker"), top_k) + [' in src, (
        "the text opens with the verdict"
    )
    assert fan.index('"tracker": t_section') < fan.index('payload["memory"]'), (
        "tracker is the first section"
    )
