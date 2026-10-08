"""`code.index_status` and `code.repos` carry the mirror's last
fetch (`clone_fetched_at`) and whether it is overdue (`fetch_overdue`), so a
client's freshness verdict — the plugin's shunt and primer decide "stale" from
`fast_tier_lag` — sees a stalled mirror, as `as_of` on every code op does;
the plugin reads the status ops once per session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.code.api.models_repos import CodeRepoSummary
from server.code.api.models_status import CodeIndexStatusOutput
from server.code.app import common as cc
from server.code.app import index_status as ctx
from server.code.app import repos
from server.settings import settings

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)


def test_fetch_freshness_reports_the_time_and_the_overdue_verdict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_fetch_stale_after_seconds", 3600, raising=False)
    fresh = NOW - timedelta(minutes=10)
    assert cc.fetch_freshness(fresh, NOW) == (fresh.isoformat(), False)
    old = NOW - timedelta(hours=3)
    assert cc.fetch_freshness(old, NOW) == (old.isoformat(), True)
    # ClickHouse hands a naive UTC datetime back; it is read as UTC
    naive = datetime(2026, 9, 24, 13, 17)
    iso, overdue = cc.fetch_freshness(naive, NOW)
    assert iso == "2026-09-24T13:17:00+00:00" and overdue is True
    # no row: unknown, never overdue (a deployment without the row keeps the lag-only verdict)
    assert cc.fetch_freshness(None, NOW) == ("", False)
    assert cc.fetch_freshness("2026-09-24", NOW) == ("", False)
    # 0 disables the verdict, the time still rides
    monkeypatch.setattr(settings, "code_fetch_stale_after_seconds", 0, raising=False)
    assert cc.fetch_freshness(old, NOW) == (old.isoformat(), False)


def test_the_status_and_the_repo_summary_carry_both_fields_with_quiet_defaults() -> None:
    st = CodeIndexStatusOutput(repo="o/r", branch="main", org="o", namespace="code-r")
    assert st.clone_fetched_at == "" and st.fetch_overdue is False
    row = CodeRepoSummary(org="o", namespace="code-r", repo="o/r", branch="main")
    assert row.clone_fetched_at == "" and row.fetch_overdue is False
    assert "fetch_overdue" in CodeIndexStatusOutput.model_fields
    assert "fetch_overdue" in CodeRepoSummary.model_fields


def test_the_status_builder_reads_the_clone_head_row_and_the_listing_copies_it() -> None:
    src = open(ctx.__file__, encoding="utf-8").read()
    body = src[src.index("async def _repo_status(") :]
    body = body[: body.index("\nasync def ", 1)]
    assert 'fetch_freshness(\n        (state.get("clone_head") or {}).get("updated_at")' in body
    assert "clone_fetched_at=clone_fetched_at," in body and "fetch_overdue=fetch_overdue," in body
    listing = open(repos.__file__, encoding="utf-8").read()
    assert (
        'getattr(st, "clone_fetched_at", "")' in listing
        and 'getattr(st, "fetch_overdue", False)' in listing
    )
