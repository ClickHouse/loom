"""A step is flagged on the failure it established, and a procedure strikes
out after repeated failures."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.domain.procedure_evidence import failure_lcb_counts
from server.memory.domain.procedure_graph import (
    CONTRAST_LAMBDA,
    CONTRAST_MARGIN,
    RunTrace,
    _fail_prone,
    contrast_threshold,
    trailing_failures,
)


def test_threshold_tightens_with_the_base_rate() -> None:
    assert contrast_threshold(0.0) == pytest.approx(CONTRAST_MARGIN)
    assert contrast_threshold(0.5) == pytest.approx(
        0.5 + CONTRAST_MARGIN * (1 - CONTRAST_LAMBDA * 0.5)
    )
    assert contrast_threshold(0.5) < 0.5 + CONTRAST_MARGIN
    assert contrast_threshold(1.0) > 1.0  # nothing stands out above a procedure that always fails


def test_one_failure_in_three_does_not_flag_a_step() -> None:
    # a point estimate (0.33 >= 0.25) would flag; the bound is 0.20 and does not
    assert failure_lcb_counts(3, 1) == pytest.approx(0.2, abs=1e-6)
    assert _fail_prone(3, 1, 0.0) is False
    assert _fail_prone(3, 2, 0.0) is True  # bound 0.40
    assert _fail_prone(3, 3, 0.0) is True  # bound 0.64
    assert _fail_prone(2, 2, 0.0) is False  # under MIN_CONTRAST_RUNS


def test_at_a_half_base_four_of_four_is_the_bar() -> None:
    assert contrast_threshold(0.5) == pytest.approx(0.6875)
    assert _fail_prone(3, 3, 0.5) is False  # bound 0.64 < 0.69 (a point rule would flag)
    assert _fail_prone(4, 4, 0.5) is True  # bound 0.69
    # fractional counts (edge weights) work the same way
    assert _fail_prone(4.0, 4.0, 0.5) is True
    assert _fail_prone(10.0, 5.0, 0.5) is False  # at the base rate


def test_trailing_failures_counts_the_current_streak() -> None:
    ok, ko = RunTrace(path=[], ok=True), RunTrace(path=[], ok=False)
    assert trailing_failures([]) == 0
    assert trailing_failures([ok, ok]) == 0
    assert trailing_failures([ok, ko, ko]) == 2
    assert trailing_failures([ko, ko, ok, ko]) == 1
    assert trailing_failures([ko, ko, ko]) == 3


# --- operation layer ---------------------------------------------------------


def _row(steps: list[str]) -> dict[str, Any]:
    return {
        "situation": "s",
        "name": "ship",
        "steps": steps,
        "tags": [],
        "metadata": {},
        "graph": None,
        "run_stats": {"total": 0, "ok": 0},
        "runs": [],
        "template": False,
        "revision": 1,
    }


def _ledger(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]) -> dict[str, Any]:
    from server.memory.app import procedure_ledger

    rec: dict[str, Any] = {"unfolded": []}

    async def _get(inp: Any) -> Any:
        return NS(value=row, content_sha256="h")

    async def _set(inp: Any) -> Any:
        row.clear()
        row.update(inp.value)
        return NS(memory_id="m", version=2)

    async def _unfolded(**kw: Any) -> list[dict[str, Any]]:
        out = list(rec["unfolded"])
        rec["unfolded"] = []
        return out

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    monkeypatch.setattr(procedure_ledger, "unfolded", _unfolded)
    return rec


def _runs(oks: list[bool], start: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "path": ["n1", "n2"],
            "ok": ok,
            "note": "",
            "revision": 1,
            "ts": f"2026-09-11 10:00:{start + i:02d}.000",
        }
        for i, ok in enumerate(oks)
    ]


def test_three_consecutive_failures_quarantine_and_a_success_lifts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput, ProcedureMatchInput
    from server.memory.app.procedure import _procedure_status, handle_procedure_match
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _row(["a", "b"])
    rec = _ledger(monkeypatch, row)
    rec["unfolded"] = _runs([True, False, False])
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=3)
        )
    )
    assert out.strikes == 2 and out.quarantined is False
    assert "quarantined" not in row

    rec["unfolded"] = _runs([False], start=3)
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=3)
        )
    )
    assert out.strikes == 3 and out.quarantined is True and out.template is False
    assert row["quarantined"]["strikes"] == 3
    assert row["quarantined"]["at"] == "2026-09-11 10:00:03.000"
    assert _procedure_status(row) == "quarantined"

    async def _search(inp: Any) -> Any:
        # two sessions' rows of the key: one procedure
        hits = [NS(namespace="ns", memory_key="p:ship", similarity=s) for s in (0.9, 0.8)]
        return NS(results=hits)

    async def _rows(_ident: Any, _keys: list[str], **_k: Any) -> list[Any]:
        return [NS(memory_key="p:ship", value=row)]

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    monkeypatch.setattr("server.memory.store.get_many_by_keys", _rows)
    m = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert m.matches == []
    m = asyncio.run(
        handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", include_quarantined=True)
        )
    )
    assert [x.status for x in m.matches] == ["quarantined"]
    assert m.matches[0].quarantined is not None and m.matches[0].quarantined["strikes"] == 3

    # a success on record lifts the quarantine
    rec["unfolded"] = _runs([True], start=4)
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=3)
        )
    )
    assert out.strikes == 0 and out.quarantined is False
    assert "quarantined" not in row


def test_strike_limit_zero_never_quarantines(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _row(["a", "b"])
    rec = _ledger(monkeypatch, row)
    rec["unfolded"] = _runs([False] * 6)
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=0)
        )
    )
    assert out.strikes == 6 and out.quarantined is False


def test_default_strike_limit_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve
    from server.settings import settings

    monkeypatch.setattr(settings, "procedure_strike_limit", 2, raising=False)
    row = _row(["a", "b"])
    rec = _ledger(monkeypatch, row)
    rec["unfolded"] = _runs([True, False, False])
    out = asyncio.run(
        handle_procedure_evolve(ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship"))
    )
    assert out.strikes == 2 and out.quarantined is True


def test_success_retention_is_the_paper_s_srr() -> None:
    from server.memory.domain.procedure_graph import success_retention

    ok, ko = RunTrace(path=[], ok=True), RunTrace(path=[], ok=False)
    assert success_retention([]) == {"pairs": 0.0, "ok_after_ok": 0.0, "rate": 0.0}
    assert success_retention([ok, ok, ok]) == {"pairs": 2.0, "ok_after_ok": 2.0, "rate": 1.0}
    # ok,ko,ok,ok,ko: ok→ko, ok→ok, ok→ko → 1 of 3
    r = success_retention([ok, ko, ok, ok, ko])
    assert (
        r["pairs"] == 3.0
        and r["ok_after_ok"] == 1.0
        and r["rate"] == pytest.approx(0.3333, abs=1e-3)
    )
    # a failure never opens a pair
    assert success_retention([ko, ko, ok])["pairs"] == 0.0


def test_evolve_reports_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _row(["a", "b"])
    rec = _ledger(monkeypatch, row)
    rec["unfolded"] = _runs([True, True, False, True])
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=0)
        )
    )
    assert out.retention == {"pairs": 2.0, "ok_after_ok": 1.0, "rate": 0.5}
