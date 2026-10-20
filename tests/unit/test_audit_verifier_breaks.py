"""The audit verifier reads its window in bounded pages and reports each
break once, across passes and restarts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.insights.workers import audit_verifier as mod
from server.settings import settings

T0 = datetime(2026, 9, 28, 10, 0, 0)


def _row(i: int, *, bad: bool = False) -> dict[str, Any]:
    return {
        "partition": 202609,
        "ts": T0 + timedelta(seconds=i),
        "request_id": f"r{i}",
        "row_hash": ("bad" if bad else "ok") + f"{i:061d}",
        "bad": bad,
    }


class _Log:
    """The audit log as the verifier sees it: rows, and its own tamper rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.pages: list[tuple[Any, int]] = []
        self.tamper: list[dict[str, Any]] = []

    async def verify_window(self, look_back, *, after=None, limit=10_000, ch=None):
        self.pages.append((after, limit))
        rows = [
            r
            for r in self.rows
            if after is None or (r["ts"], r["request_id"], r["row_hash"]) > after
        ]
        return rows[:limit]

    async def reported_tamper_keys(self, since_seconds, *, ch=None):
        return {k for t in self.tamper for k in t["subject"]["break_keys"]}

    async def emit(self, event_type, **kw):
        # what the row stores: the subject as JSON
        self.tamper.append(
            {"event_type": event_type, "subject": json.loads(json.dumps(kw["subject"]))}
        )
        return "h"


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    from server.insights.store import audit as audit_repo

    lg = _Log([])
    monkeypatch.setattr(audit_repo, "verify_window", lg.verify_window)
    monkeypatch.setattr(audit_repo, "reported_tamper_keys", lg.reported_tamper_keys)
    monkeypatch.setattr(mod.audit, "emit", lg.emit)
    monkeypatch.setattr(mod, "row_matches_hash", lambda r: not r["bad"])

    async def _no_checkpoints(cfg: Any, result: Any, **_: Any) -> None:
        return None  # the checkpoint leg has its own tests; these pin the per-row leg

    monkeypatch.setattr(mod, "run_checkpoints", _no_checkpoints)
    return lg


def _cfg() -> mod.AuditVerifierConfig:
    return mod.AuditVerifierConfig()


@pytest.mark.asyncio
async def test_the_window_is_read_in_pages_by_keyset(log: _Log, monkeypatch) -> None:
    monkeypatch.setattr(mod, "VERIFY_PAGE_ROWS", 3)
    log.rows = [_row(i) for i in range(7)]
    res = await mod.run_pass(_cfg())
    assert res.rows_checked == 7 and not res.truncated
    assert [a for a, _ in log.pages] == [
        None,
        (T0 + timedelta(seconds=2), "r2", log.rows[2]["row_hash"]),
        (T0 + timedelta(seconds=5), "r5", log.rows[5]["row_hash"]),
    ]


@pytest.mark.asyncio
async def test_a_window_past_the_cap_is_reported_truncated(log: _Log, monkeypatch) -> None:
    monkeypatch.setattr(mod, "VERIFY_PAGE_ROWS", 2)
    monkeypatch.setattr(settings, "audit_verify_max_rows_per_pass", 4)
    log.rows = [_row(i) for i in range(9)]
    res = await mod.run_pass(_cfg())
    assert res.rows_checked == 4 and res.truncated and len(log.pages) == 2


@pytest.mark.asyncio
async def test_a_break_is_reported_once_and_a_new_one_still_is(log: _Log) -> None:
    log.rows = [_row(0), _row(1, bad=True), _row(2)]
    first = await mod.run_pass(_cfg())
    again = await mod.run_pass(_cfg())  # same break, still in the window
    assert first.new_breaks == 1 and again.new_breaks == 0 and len(again.breaks) == 1
    assert len(log.tamper) == 1
    assert log.tamper[0]["subject"]["break_keys"] == [mod.break_key(first.breaks[0])]
    log.rows.append(_row(3, bad=True))
    third = await mod.run_pass(_cfg())
    assert third.new_breaks == 1 and len(log.tamper) == 2
    assert log.tamper[1]["subject"]["break_count"] == 1
    assert log.tamper[1]["subject"]["first_breaks"][0]["request_id"] == "r3"


@pytest.mark.asyncio
async def test_a_checkpoint_break_is_reported_once(log: _Log, monkeypatch) -> None:
    async def _deletion(cfg, result, **_):
        result.checkpoint_breaks.append({"window_start": "2026-09-28T09:00:00", "kind": "deletion"})

    monkeypatch.setattr(mod, "run_checkpoints", _deletion)
    cfg = mod.AuditVerifierConfig()
    await mod.run_pass(cfg)
    await mod.run_pass(cfg)
    assert len(log.tamper) == 1
    assert log.tamper[0]["subject"]["break_keys"] == ["deletion:2026-09-28T09:00:00"]


@pytest.mark.asyncio
async def test_an_unreadable_history_reports_every_break(log: _Log, monkeypatch) -> None:
    from server.insights.store import audit as audit_repo

    async def _fails(*a, **k):
        raise RuntimeError("replica down")

    log.rows = [_row(1, bad=True)]
    await mod.run_pass(_cfg())
    monkeypatch.setattr(audit_repo, "reported_tamper_keys", _fails)
    res = await mod.run_pass(_cfg())
    assert res.new_breaks == 1 and len(log.tamper) == 2  # fail loud, not silent


def test_the_history_read_uses_the_sort_key_prefix() -> None:
    import inspect

    from server.insights.store import audit as audit_repo

    src = inspect.getsource(audit_repo.reported_tamper_keys)
    assert "org = '-' AND namespace = '-' AND event_type = {et:String}" in src
