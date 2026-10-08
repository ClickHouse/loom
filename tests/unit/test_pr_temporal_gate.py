"""Unit tests for the memory.ask PR-temporal gate (_gate_future_prs): drop
grounding memories that cite a PR opened after the as_of anchor. Uses a fake
CodeRepo so no DB is needed."""

from __future__ import annotations

import asyncio
from datetime import datetime

import server.memory.app.code_grounding as MA


class _M:
    def __init__(self, key, content):
        self.memory_key = key
        self.content_excerpt = content


class _SI:
    def __init__(self, as_of, ns="code-clickhouse", org="clickhouse"):
        self.as_of = as_of
        self.code_namespace = ns
        self.code_org = org
        self.namespace = "demo"
        self.org = org


def _run(mems, as_of, opened, items=None):
    """opened: {pr_number: datetime opened_at} (merged-PR plane);
    items: {number: datetime opened_at} (issues / open PRs plane)."""
    items = items or {}

    class _Repo:
        async def prs_by_number(self, org, ns, numbers):
            return {
                n: {"opened_at": opened.get(n), "author": "", "title": "", "merged_at": None}
                for n in numbers
                if n in opened
            }

        async def items_by_number(self, org, ns, numbers):
            return {n: {"opened_at": items.get(n), "is_pr": False} for n in numbers if n in items}

    import server.code.store.index as cr

    real = cr.CodeRepo
    cr.CodeRepo = _Repo  # type: ignore[assignment,misc]
    try:
        return asyncio.run(_gate(mems, as_of))
    finally:
        cr.CodeRepo = real  # type: ignore[assignment,misc]


async def _gate(mems, as_of):
    return await MA._gate_future_prs(mems, _SI(as_of), _settings_stub())


class _settings_stub:
    memory_ask_code_namespace = "code-clickhouse"
    memory_ask_code_org = "clickhouse"
    code_org = "clickhouse"


def _keys(ms):
    return [m.memory_key for m in ms]


def test_drops_memory_citing_future_pr():
    mems = [_M("a", "PR #117827 landed the fix"), _M("b", "unrelated note")]
    out = _run(mems, datetime(2026, 9, 1), {117827: datetime(2026, 9, 5)})
    assert _keys(out) == ["b"]  # #117827 opened after anchor -> dropped


def test_keeps_memory_when_pr_predates_anchor():
    mems = [_M("a", "PR #117827 landed the fix")]
    out = _run(mems, datetime(2026, 9, 10), {117827: datetime(2026, 9, 5)})
    assert _keys(out) == ["a"]  # opened before anchor -> kept


def test_unknown_open_date_never_gated():
    mems = [_M("a", "PR #999999 something")]
    out = _run(mems, datetime(2026, 9, 1), {})  # no opened_at known
    assert _keys(out) == ["a"]  # conservative: don't drop what we can't date


def test_no_as_of_is_noop():
    mems = [_M("a", "PR #117827 x")]
    out = _run(mems, None, {117827: datetime(2026, 9, 5)})
    assert _keys(out) == ["a"]


def test_epoch_default_not_treated_as_future():
    # opened_at defaulting to 1970 (unpopulated column) must not gate.
    mems = [_M("a", "PR #117827 x")]
    out = _run(mems, datetime(2026, 9, 1), {117827: datetime(1970, 1, 1)})
    assert _keys(out) == ["a"]


def test_issue_opened_after_anchor_is_gated_via_items_plane():
    """The merged-PR plane never knows an issue; loom.code_gh_items does."""
    mems = [_M("a", "issue #118000 tracks the regression"), _M("b", "unrelated")]
    out = _run(mems, datetime(2026, 9, 1), {}, items={118000: datetime(2026, 9, 3)})
    assert _keys(out) == ["b"]
    out = _run(mems, datetime(2026, 9, 5), {}, items={118000: datetime(2026, 9, 3)})
    assert _keys(out) == ["a", "b"], "existed at the anchor -> kept"


def test_describe_item_states_kind_opener_and_state_as_of():
    from datetime import date

    from server.memory.app.code_grounding import describe_item

    row = {
        "is_pr": False,
        "author": "alice",
        "title": "Crash in X",
        "opened_at": datetime(2026, 8, 1),
        "closed_at": datetime(2026, 8, 20),
        "merged_at": None,
        "state_reason": "completed",
    }
    assert (
        describe_item(5, row, date(2026, 8, 10))
        == "[github] #5 is an issue (not a PR) opened by @alice on 2026-08-01 still open as of 2026-08-10 — Crash in X"
    )
    assert "closed 2026-08-20 (completed)" in describe_item(5, row, date(2026, 9, 1))
    assert describe_item(5, row, date(2026, 7, 1)) == "", "did not exist yet: the gate's job"
    pr = {
        "is_pr": True,
        "author": "bob",
        "title": "",
        "opened_at": datetime(2026, 8, 1),
        "closed_at": datetime(2026, 8, 9),
        "merged_at": None,
    }
    assert describe_item(7, pr, None).endswith("closed 2026-08-09 without merging")
    merged = dict(pr, merged_at=datetime(2026, 8, 9))
    assert "merged 2026-08-09" in describe_item(7, merged, date(2026, 8, 9))
