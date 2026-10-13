"""Caller-shaped reads run as the read-only ClickHouse user the shipped config
defines (`loom_reader`), which can read but not write, change schema or grant.
Skips unless LOOM_CH_HOST and LOOM_CH_READER_USER are set."""

from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
async def client() -> Any:
    if not os.environ.get("LOOM_CH_READER_USER"):
        pytest.skip("LOOM_CH_READER_USER is not set")
    from server.core.ch import CHClient

    c = CHClient()
    await c.connect()
    # a grantee that exists, so a GRANT fails for want of the right, not the name
    await c.execute("CREATE ROLE IF NOT EXISTS reader_probe_role")
    yield c
    await c.execute("DROP ROLE IF EXISTS reader_probe_role")
    await c.disconnect()


async def test_caller_shaped_reads_run_as_the_reader(client: Any) -> None:
    from server.core.ch import caller_shaped

    reader = os.environ["LOOM_CH_READER_USER"]
    assert (await client.query_one("SELECT currentUser() AS u"))["u"] != reader
    with caller_shaped():
        row = await client.query_one("SELECT currentUser() AS u, count() AS n FROM loom.memory")
    assert row["u"] == reader


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE loom.reader_probe (x Int32) ENGINE = MergeTree ORDER BY x",
        "ALTER TABLE loom.memory DELETE WHERE org = 'reader-probe'",
        "INSERT INTO loom.agent_events (org) VALUES ('reader-probe')",
        "GRANT SELECT ON loom.* TO reader_probe_role",
        "CREATE USER reader_probe IDENTIFIED BY 'x'",
    ],
)
async def test_a_caller_shaped_statement_cannot_write_or_grant(client: Any, sql: str) -> None:
    from server.core.ch import caller_shaped

    with caller_shaped(), pytest.raises(Exception, match=r"ACCESS_DENIED|READONLY"):
        await client.query_all(sql)


async def test_the_reader_cannot_read_the_query_log(client: Any) -> None:
    from server.core.ch import caller_shaped

    with caller_shaped(), pytest.raises(Exception, match="ACCESS_DENIED"):
        await client.query_all("SELECT count() FROM system.query_log")


async def test_a_read_over_its_memory_cap_is_a_422(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.ch import caller_shaped
    from server.core.errors import ValidationError
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_caller_shaped_max_memory_bytes", 5_000_000)
    with caller_shaped(), pytest.raises(ValidationError, match="narrow it"):
        await client.query_all("SELECT groupArray(number) AS a FROM numbers(10000000)")


@pytest.mark.parametrize("cap", ["max_execution_time", "max_memory_usage", "max_rows_to_read"])
async def test_a_reader_statement_cannot_lift_a_cap_with_0(client: Any, cap: str) -> None:
    """0 means no limit in ClickHouse; the reader profile refuses it, even from a
    SETTINGS clause in the SQL (which wins over the URL's ceiling)."""
    from server.core.ch import caller_shaped

    with caller_shaped(), pytest.raises(Exception, match=r"Code: 452"):
        await client.query_all(f"SELECT count() FROM numbers(10) SETTINGS {cap} = 0")


@pytest.mark.parametrize("limit_s", [0.01, -1])
async def test_a_reader_statement_cannot_go_under_the_time_floor(
    client: Any, limit_s: float
) -> None:
    """The time min is code.grep's statement floor (0.05 s)."""
    from server.core.ch import caller_shaped

    sql = f"SELECT count() FROM numbers(10) SETTINGS max_execution_time = {limit_s}"
    with caller_shaped(), pytest.raises(Exception, match=r"shouldn't be less than 0.05"):
        await client.query_all(sql)


@pytest.mark.parametrize("limit_s", [0.05, 0.5, 1.25])
async def test_a_deadline_bounded_grep_scan_runs_as_the_reader(client: Any, limit_s: float) -> None:
    """code.grep with deadline_ms bounds each scan below a second."""
    from server.code.domain.search_scope import build_scope
    from server.code.store.index import CodeRepo
    from server.core.ch import caller_shaped

    with caller_shaped():
        hits = await CodeRepo(client).grep_chunks(
            "reader-probe",
            "none",
            "needle",
            scope=build_scope(pattern="needle"),
            max_execution_s=limit_s,
        )
    assert list(hits) == []


def test_code_grep_with_a_deadline_answers_over_http(control_plane_url: str) -> None:
    """The server under test runs its caller-shaped reads as LOOM_CH_READER_USER."""
    from tests.integration._resources import unique_org
    from tests.integration.test_read_scope_isolation import _mint, _post

    if not os.environ.get("LOOM_CH_READER_USER"):
        pytest.skip("LOOM_CH_READER_USER is not set")
    org, ns = unique_org("grep-deadline"), "code-reader-probe"
    body = {"org": org, "namespace": ns, "agent": "a", "pattern": "needle", "deadline_ms": 300}
    r = _post(control_plane_url, _mint(org, ns), "code.grep", body)
    assert r.status_code == 200, r.text[:400]


async def test_a_ceiling_above_the_reader_profile_fails_the_boot_probe(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ch
    from server.settings import settings

    monkeypatch.setattr(ch, "_reader_user_ok", None)
    monkeypatch.setattr(ch, "_reader_user_error", "")
    await ch.probe_reader_user(client)
    assert ch.reader_user_state() is True
    monkeypatch.setattr(settings, "ch_caller_shaped_max_rows_to_read", 600_000_000)
    await ch.probe_reader_user(client)
    assert ch.reader_user_state() is False
    assert "Code: 452" in ch.reader_user_error()
