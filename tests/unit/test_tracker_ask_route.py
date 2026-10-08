"""The QA-intent route of memory.ask: a test, a release
minor next to a release word, or a component's risk is classified
deterministically; the answer is a record keyed to the tracker namespace; every
missing binding, unavailable source or empty record is None (unknown), and
the route is reached from the gh-items fallback when no number is named."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import server.tracker.app.ask_route as r
import server.tracker.app.binding as binding
from server.settings import settings
from server.tracker.domain.errors import TrackerUnavailable

ORG, CODE_NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"


def test_classify_the_three_shapes_and_nothing_else() -> None:
    assert r.classify("is 02345_keeper_session flaky?") == ("flaky", "02345_keeper_session")
    assert r.classify("02345_keeper_session") == ("flaky", "02345_keeper_session")
    assert r.classify("is release 26.3 ready to ship? any blockers") == ("release", "26.3")
    assert r.classify("how risky is Keeper right now") == ("component", "Keeper")
    assert r.classify("open bugs in MergeTree") == ("component", "MergeTree")
    assert r.classify("what did PR #119663 change") == ("", "")
    assert r.classify("26.3 changed the default of max_threads") == ("", "")  # no release word


class _Source:
    def __init__(self, *, flaky=None, release=None, risk=None, issues=None, down=False) -> None:
        self.flaky, self.release, self.risk, self.issues, self.down = (
            flaky,
            release,
            risk,
            issues,
            down,
        )

    async def flaky_issues_matching(self, org, ns, name, *, limit=10):
        if self.down:
            raise TrackerUnavailable("OPEN")
        return list(self.flaky or [])

    async def release_items(self, org, ns, version):
        return list(self.release or [])

    async def component_risk_latest(self, org, ns, comps):
        return dict(self.risk or {})

    async def open_issues_by_component(self, org, ns, component, *, limit=10):
        return list(self.issues or [])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(settings, "memory_ask_code_namespace", CODE_NS, raising=False)
    monkeypatch.setattr(settings, "memory_ask_code_org", ORG, raising=False)

    async def _authorized(org, code_ns):
        return QA if code_ns == CODE_NS else ""

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    holder: dict[str, Any] = {}
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])

    def _use(src: _Source) -> _Source:
        holder["src"] = src
        return src

    return _use


def _inp(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "org": ORG,
        "namespace": "demo",
        "code_namespace": "",
        "code_org": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_flaky_question_answers_from_the_record(wired) -> None:
    wired(
        _Source(
            flaky=[
                {
                    "number": 9,
                    "title": "02345_keeper_session is flaky",
                    "state": "open",
                    "opened_at": datetime(2026, 9, 3),
                    "closed_at": None,
                },
                {
                    "number": 4,
                    "title": "Flaky 02345_keeper_session",
                    "state": "closed",
                    "opened_at": datetime(2026, 6, 1),
                    "closed_at": datetime(2026, 6, 9),
                },
            ]
        )
    )
    out = await r.qa_route_answer("is 02345_keeper_session flaky?", _inp(), settings, [], "m")
    assert out is not None and out.source == "record" and not out.abstain
    assert out.answer.startswith("02345_keeper_session: known flaky — 1 open flaky-test issue of 2")
    assert "#9 (open, opened 2026-09-03)" in out.answer and "closed 2026-06-09" in out.answer
    assert out.memory_keys == [f"tracker_flaky:{QA}:02345_keeper_session"]


@pytest.mark.asyncio
async def test_release_and_component_shapes(wired) -> None:
    wired(
        _Source(
            release=[
                {
                    "item_kind": "blocker",
                    "item_ref": "ClickHouse/ClickHouse#5",
                    "status": "Blockers",
                    "issue_state": "OPEN",
                    "fix_version": "26.3.2",
                },
                {
                    "item_kind": "blocker",
                    "item_ref": "ClickHouse/ClickHouse#6",
                    "status": "Merged",
                    "issue_state": "CLOSED",
                    "fix_version": "",
                },
                {
                    "item_kind": "health",
                    "snapshot_date": datetime(2026, 9, 15).date(),
                    "open_blockers": 1,
                    "critical_untested": 2,
                    "total_items": 9,
                },
            ],
            risk={
                "Keeper": {
                    "snapshot_date": datetime(2026, 9, 15).date(),
                    "total_bugs": 40,
                    "open_bugs": 3,
                    "gap_ratio": 0.25,
                    "risk_score": 12.5,
                    "trend_delta": -0.05,
                }
            },
            issues=[
                {
                    "number": 7,
                    "title": "Keeper crash",
                    "severity_level": "critical",
                    "bisect_outcome": "CONVERGED",
                }
            ],
        )
    )
    out = await r.qa_route_answer("is 26.3 ready to ship? blockers?", _inp(), settings, [], "m")
    assert out is not None and out.answer.startswith(
        "Release 26.3: 1 active blocker of 2 tracked items."
    )
    assert "ClickHouse/ClickHouse#5: blocker, open (fix version 26.3.2)" in out.answer
    assert "2 critical items shipped without a test" in out.answer
    out = await r.qa_route_answer("how risky is Keeper", _inp(), settings, [], "m")
    assert (
        out is not None
        and "Keeper (risk snapshot 2026-09-15): 40 bugs in two years, 3 open" in out.answer
    )
    assert (
        "gap ratio 0.25" in out.answer
        and "#7 [critical, bisect CONVERGED] Keeper crash" in out.answer
    )
    assert out.memory_keys == [f"tracker_component:{QA}:Keeper"]


@pytest.mark.asyncio
async def test_absence_unbound_unavailable_and_off_are_none(wired, monkeypatch) -> None:
    wired(_Source(flaky=[]))
    assert (
        await r.qa_route_answer("is 02345_keeper_session flaky?", _inp(), settings, [], "m") is None
    )
    wired(_Source(down=True))
    assert (
        await r.qa_route_answer("is 02345_keeper_session flaky?", _inp(), settings, [], "m") is None
    )
    wired(_Source(flaky=[{"number": 1, "title": "t", "state": "open", "opened_at": None}]))
    assert (
        await r.qa_route_answer(
            "is 02345_keeper_session flaky?", _inp(code_namespace="code-other"), settings, [], "m"
        )
        is None
    )
    monkeypatch.setattr(settings, "memory_ask_code_namespace", "", raising=False)
    assert (
        await r.qa_route_answer("is 02345_keeper_session flaky?", _inp(), settings, [], "m") is None
    )


@pytest.mark.asyncio
async def test_gh_items_fallback_reaches_the_route_when_no_number_is_named(monkeypatch) -> None:
    from server.memory.app.ask import records

    seen: list[str] = []

    async def _route(question, search_input, settings_, results, model):
        seen.append(question)
        return "routed"

    monkeypatch.setattr(r, "qa_route_answer", _route)
    out = await records._gh_items_fallback_answer(
        "is 02345_keeper_session flaky?", _inp(), settings, [], "m", None
    )
    assert out == "routed" and seen == ["is 02345_keeper_session flaky?"]
