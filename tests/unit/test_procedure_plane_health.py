"""ops.health `procedure_plane`: fold, assess, query scope."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import procedure_plane_health as pp


def _plane(**kw: Any) -> pp.ProcedurePlane:
    return pp.fold({**kw}, window_days=7)


def test_fold_reads_the_aggregate_row_and_derives_rates() -> None:
    p = _plane(packs=718, packs_showing=0, packs_withholding=0, ledger_rows=0, active_procedures=80)
    assert p.show_rate == 0.0 and p.success_rate is None and p.retention is None
    q = _plane(
        packs=100,
        packs_showing=40,
        ledger_rows=20,
        ledger_ok=15,
        retention_pairs=10,
        retention_ok_after_ok=8,
        quarantined=1,
        active_procedures=5,
    )
    assert q.show_rate == pytest.approx(0.4) and q.success_rate == pytest.approx(0.75)
    assert q.retention == pytest.approx(0.8)
    assert q.as_data()["retention"] == 0.8 and q.as_data()["quarantined"] == 1
    assert pp.fold(None, window_days=3).packs == 0


def test_assess_warns_on_the_dark_plane() -> None:
    p = _plane(packs=718, packs_showing=0, active_procedures=80)
    status, detail, repair = pp.assess(p, min_packs=50, retention_warn=0.5, min_pairs=20)
    assert status == "warn" and "718 packs" in detail and "no procedure" in detail
    assert "situation" in repair


def test_assess_is_quiet_when_there_are_no_procedures_or_too_few_packs() -> None:
    none = _plane(packs=718, packs_showing=0, active_procedures=0)
    assert pp.assess(none, min_packs=50, retention_warn=0.5, min_pairs=20)[0] == "ok"
    few = _plane(packs=10, packs_showing=0, active_procedures=80)
    status, detail, _ = pp.assess(few, min_packs=50, retention_warn=0.5, min_pairs=20)
    assert status == "ok" and "shape not judged" in detail


def test_assess_warns_on_low_retention_over_enough_pairs() -> None:
    p = _plane(
        packs=100,
        packs_showing=30,
        ledger_rows=40,
        ledger_ok=20,
        retention_pairs=25,
        retention_ok_after_ok=9,
        active_procedures=4,
    )
    status, detail, repair = pp.assess(p, min_packs=50, retention_warn=0.5, min_pairs=20)
    assert status == "warn" and "retention 36%" in detail and "revise" in repair
    thin = _plane(
        packs=100, packs_showing=30, retention_pairs=5, retention_ok_after_ok=1, active_procedures=4
    )
    assert pp.assess(thin, min_packs=50, retention_warn=0.5, min_pairs=20)[0] == "ok"


def test_assess_healthy_detail_names_the_numbers() -> None:
    p = _plane(
        packs=200,
        packs_showing=90,
        packs_withholding=10,
        ledger_rows=60,
        ledger_ok=50,
        procedures_run=7,
        retention_pairs=40,
        retention_ok_after_ok=36,
        quarantined=2,
        active_procedures=12,
    )
    status, detail, _ = pp.assess(p, min_packs=50, retention_warn=0.5, min_pairs=20)
    assert status == "ok"
    assert "200 packs, 90 showed a procedure (45%)" in detail
    assert "60 ledger rows over 7 procedures" in detail
    assert "retention 90% over 40 pairs" in detail and "2 quarantined" in detail


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((sql, params))
        return {
            "packs": 3,
            "packs_showing": 1,
            "ledger_rows": 2,
            "ledger_ok": 2,
            "procedures_run": 1,
            "retention_pairs": 1,
            "retention_ok_after_ok": 1,
            "quarantined": 0,
            "active_procedures": 2,
            "packs_withholding": 0,
        }


@pytest.mark.asyncio
async def test_query_scopes_org_window_and_namespace() -> None:
    ch = _CH()
    p = await pp.procedure_plane(ch, org="o", namespace="n", window_days=3)
    sql, params = ch.calls[0]
    assert params == {"org": "o", "days": 3, "ns": "n"}
    assert (
        "loom.agent_events" in sql and "loom.procedure_runs" in sql and "loom.memory FINAL" in sql
    )
    assert "out_procedure_keys_withheld" in sql and "JSONHas(value, 'quarantined')" in sql
    assert p.packs == 3 and p.retention == 1.0
    ch2 = _CH()
    await pp.procedure_plane(ch2, org="o")
    assert ch2.calls[0][1]["ns"] == "*"
