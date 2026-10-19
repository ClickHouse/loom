"""The number route carries the tracker record beside the PR record:
history items gain `qa`, code.ask renders a `[qa]` line, memory.ask's
record fallback appends it — all additive, all silent when unbound."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import server.code.app.history as ch
import server.tracker.app.binding as binding
import server.tracker.app.facts as facts

FACT = {
    "kind": "issue",
    "component": "Keeper",
    "bisect_outcome": "CONVERGED",
    "reproduces_on": ["26.4"],
    "fixed_by": [200],
}


class _Repo:
    async def items_by_number(self, org, ns, nums):
        return {
            100: {
                "is_pr": 0,
                "state": "open",
                "title": "Keeper crash",
                "author": "u",
                "opened_at": datetime(2026, 9, 1),
            }
        }

    async def prs_by_number(self, org, ns, nums, **k):
        return {}

    async def prs_linking_issue(self, org, ns, nums, **k):
        return []


@pytest.fixture
def qa(monkeypatch):
    async def _authorized(org, code_ns, *, hides_answer=True):
        return "tracker-clickhouse" if code_ns.startswith("code-clickhouse") else ""

    async def _facts(org, tracker_ns, nums):
        return {100: dict(FACT)} if tracker_ns else {}

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    monkeypatch.setattr(facts, "tracker_facts_for_numbers", _facts)


def test_history_items_gain_tracker(qa) -> None:
    section = asyncio.run(
        ch.history_for_numbers(_Repo(), "clickhouse", "code-clickhouse", [100, 7])
    )
    items = {it["number"]: it for it in section["items"]}
    assert items[100]["tracker"] == FACT and items[100]["kind"] == "issue"
    assert "qa" not in items[7] and items[7]["kind"] == "unknown"


def test_unbound_namespace_adds_nothing(qa) -> None:
    section = asyncio.run(ch.history_for_numbers(_Repo(), "clickhouse", "code-other", [100]))
    assert "tracker" not in section["items"][0]


def test_code_ask_history_lines_render_the_tracker_line() -> None:
    from server.code.app.answers import history_lines

    lines = history_lines(
        {
            "items": [
                {
                    "number": 100,
                    "kind": "issue",
                    "state": "open",
                    "title": "Keeper crash",
                    "tracker": FACT,
                }
            ]
        }
    )
    assert lines[0].startswith("#100 is an issue")
    assert (
        lines[1]
        == "[tracker] #100: component Keeper; bisect CONVERGED; still reproduces on 26.4; fixed by #200."
    )


@pytest.mark.asyncio
async def test_memory_ask_fallback_appends_the_tracker_line(qa, monkeypatch) -> None:
    from server.memory.app.ask import records

    class _CodeRepo:
        def __init__(self, ch=None):
            pass

        async def items_by_number(self, org, ns, nums):
            return {
                100: {
                    "is_pr": 0,
                    "author": "u",
                    "title": "Keeper crash",
                    "opened_at": datetime(2026, 9, 1),
                    "closed_at": None,
                    "merged_at": None,
                }
            }

        async def prs_by_number(self, org, ns, nums, **k):
            return {}

    import server.code.store.index as cr

    monkeypatch.setattr(cr, "CodeRepo", _CodeRepo)
    st: Any = SimpleNamespace(
        memory_ask_code_namespace="code-clickhouse",
        memory_ask_code_org="clickhouse",
        code_org="clickhouse",
    )
    inp = SimpleNamespace(
        org="clickhouse", namespace="mem", code_namespace="", code_org="", as_of=None
    )
    out = await records._gh_items_fallback_answer("what is #100?", inp, st, [], "m", None)
    assert out is not None and out.source == "record"
    assert "[tracker] #100: component Keeper" in out.answer
    assert (
        "tracker_item:tracker-clickhouse:100" in out.memory_keys
        and "gh_item:code-clickhouse:100" in out.memory_keys
    )
