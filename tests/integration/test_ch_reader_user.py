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
    """0 means no limit in ClickHouse; the reader profile refuses it."""
    from server.core.ch import caller_shaped

    with caller_shaped(), pytest.raises(Exception, match=r"Code: 452"):
        await client.query_all(f"SELECT count() FROM numbers(10) SETTINGS {cap} = 0")
