"""Onboarding a tracker against a real ClickHouse.

1. A user made read-only the usual way (`SETTINGS readonly = 1`) refuses every
   per-query setting, and loom sets `readonly = 2` and a timeout on each
   read: the Check names the fix instead of saying "unreachable".
2. The schema check reads `system.columns` only: plain views pass it and then
   every read is ACCESS_DENIED (a plain view reads with the reader's rights).
   The Check reads one row through each required view and says so.
3. A tracker added from the page is a configured tracker namespace on every
   process, not only one that refreshed its registry snapshot: every process
   keeps it fresh.
4. The templates create DEFINER views and derive `id` with `loom_uuid5`,
   which equals Python's uuid5 (checked on ClickHouse 26.5)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import server.tracker.app.onboarding as ob
from server.core.errors import ValidationError
from server.tracker.app.sources import handle_tracker_source_add
from server.tracker.domain import contract as rc
from tests.unit.test_tracker_source_add import (  # noqa: F401 — _cfg is the autouse fixture
    RO_GRANTS,
    _add_input,
    _cfg,
    _full_schema,
)


def _probe_result(unreadable: dict[str, str] | None = None):
    async def _fake(**kw: Any) -> ob.ProbeResult:
        return ob.ProbeResult(
            grants=ob.review_grants(RO_GRANTS, [{"role_name": "analyst"}]),
            described=_full_schema(),
            unreadable=dict(unreadable or {}),
        )

    return _fake


def test_readonly_mode_is_recognised_from_the_clickhouse_error() -> None:
    e = RuntimeError(
        "Code: 164. DB::Exception: Cannot modify 'readonly' setting in readonly mode. (READONLY)"
    )
    assert ob._is_readonly_mode(e)
    assert not ob._is_readonly_mode(RuntimeError("Code: 516. Authentication failed"))


@pytest.mark.asyncio
async def test_a_readonly_1_user_is_told_the_fix(_cfg, monkeypatch) -> None:  # noqa: F811
    async def refused(**kw: Any) -> ob.ProbeResult:
        raise ob.ReadonlyModeError(
            "'loom_ro' has readonly = 1 … Run: ALTER USER loom_ro SETTINGS readonly = 2"
        )

    monkeypatch.setattr(ob, "probe_credential", refused)
    with pytest.raises(ValidationError) as e:
        await handle_tracker_source_add(_add_input(dry_run=True))
    assert e.value.to_detail()["code"] == "CREDENTIAL_READONLY_MODE"
    assert "readonly = 2" in str(e.value)


@pytest.mark.asyncio
async def test_views_that_cannot_be_read_through_are_refused_even_in_a_dry_run(
    _cfg,  # noqa: F811
    monkeypatch,
) -> None:
    ch = _cfg
    monkeypatch.setattr(
        ob,
        "probe_credential",
        _probe_result(
            {
                "github_issues": "access denied to what the view reads",
                "edges": "access denied to what the view reads",
            }
        ),
    )
    for dry in (True, False):
        with pytest.raises(ValidationError) as e:
            await handle_tracker_source_add(_add_input(dry_run=dry))
        assert e.value.to_detail()["code"] == "VIEWS_NOT_READABLE"
        assert "SQL SECURITY DEFINER" in str(e.value) and "github_issues" in str(e.value)
    assert "loom.qa_sources" not in ch.inserted
    # an optional table that cannot be read does not block (it is simply not provided)
    monkeypatch.setattr(ob, "probe_credential", _probe_result({"core_incidents": "access denied"}))
    out = await handle_tracker_source_add(_add_input(dry_run=True))
    assert out.contract["ok"] is True


@pytest.mark.asyncio
async def test_the_probe_reads_one_row_through_each_described_contract_table() -> None:
    seen: list[str] = []

    async def query(sql: str, params: Any = None) -> list[dict[str, Any]]:
        seen.append(sql)
        if "`github_prs`" in sql:
            raise RuntimeError(
                "Code: 497. DB::Exception: loom_ro: Not enough privileges. (ACCESS_DENIED)"
            )
        return [{"1": 1}]

    described = {
        "github_issues": {"id": "UUID"},
        "github_prs": {"id": "UUID"},
        "unrelated": {"x": "String"},
    }
    out = await ob._read_one_row_each(query, "db", described, 1.0)
    assert out == {"github_prs": "access denied to what the view reads"}
    assert seen == [
        "SELECT 1 FROM `db`.`github_issues` LIMIT 1",
        "SELECT 1 FROM `db`.`github_prs` LIMIT 1",
    ]


def test_the_uuid5_expression_is_the_rfc_construction() -> None:
    sql = rc.uuid5_sql("name")
    assert "6ba7b8109dad11d180b400c04fd430c8" == uuid.NAMESPACE_DNS.hex
    assert (
        "bitOr(bitAnd(reinterpretAsUInt8(substring(" in sql
        and ", 15), 80)" in sql
        and ", 63), 128)" in sql
    )
    assert rc.uuid5_function_sql().startswith(
        "CREATE FUNCTION IF NOT EXISTS loom_uuid5 AS (name) -> toUUID("
    )


def test_templates_are_definer_views_with_derived_ids() -> None:
    t = rc.catalogue("loom_tracker")["templates"]
    for table, sql in t.items():
        assert "DEFINER = CURRENT_USER SQL SECURITY DEFINER AS" in sql, table
    issues = t["github_issues"]
    assert issues.startswith("-- ids: loom derives uuid5 ids locally")
    assert (
        "loom_uuid5(concat(if(is_private = 1, 'github_issue_private:', 'github_issue:'), source_id)) AS id,"
        in issues
    )
    assert "loom_uuid5(concat(from_type, ':', <from_source_id>)) AS from_id," in t["edges"]
    assert "loom_uuid5" not in t["checks"]  # only the tables that carry ids declare the function


def test_every_template_still_separates_columns_before_the_comment() -> None:
    for table, sql in rc.catalogue("db")["templates"].items():
        body = sql.split("\nSELECT\n", 1)[1].rsplit("\nFROM ", 1)[0]
        cols = [line.split("--", 1)[0].rstrip() for line in body.splitlines()]
        assert all(c.endswith(",") for c in cols[:-1]) and not cols[-1].endswith(","), table


@pytest.mark.asyncio
async def test_every_process_keeps_the_registry_fresh(monkeypatch) -> None:
    import asyncio

    from server import boot
    from server.tracker.app import registry_sources

    calls: list[bool] = []

    async def refresh(*, force: bool = False, ch: Any = None) -> list:
        calls.append(force)
        return []

    monkeypatch.setattr(registry_sources, "refresh_db_sources", refresh)
    monkeypatch.setattr(boot.settings, "tracker_sources_refresh_seconds", 0.0)
    real_sleep = asyncio.sleep

    async def fast_sleep(s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(boot.asyncio, "sleep", fast_sleep)
    task = boot.start_tracker_sources_refresher(object())
    for _ in range(20):
        await real_sleep(0)
    task.cancel()
    assert (
        calls[0] is True and len(calls) >= 2 and calls[1] is False
    )  # forced at boot, then the TTL
