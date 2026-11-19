"""Compiler reconciliation reads oversized symbol names through real ClickHouse."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.code.store.index import CodeRepo
from server.code.store.index.rows import build_row
from server.core.ch import CHClient, query_tag
from server.core.datetimes import naive_utc_now
from tests.integration._resources import unique_org

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("ch_target_required")]


def _value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode().rstrip("\x00")
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value


@pytest.mark.parametrize("round_read", [False, True])
@pytest.mark.parametrize(
    "large_name", ["型\\'" * 40_000, "型" * 720_000], ids=["quoted-multibyte", "many-parts"]
)
async def test_large_symbol_keys_round_trip_every_column(round_read: bool, large_name: str) -> None:
    org, namespace = unique_org("code-reconciliation"), "code-parameters"
    stamp = naive_utc_now().replace(microsecond=0)
    rows = [
        build_row(
            "code_symbols",
            org=org,
            namespace=namespace,
            tier="scip",
            qualified_name=name,
            symbol_id=f"{i:032x}",
            name=f"function_{i}",
            kind="function",
            signature="()",
            path="main.py",
            start_line=i + 1,
            end_line=i + 2,
            indexed_commit="a" * 40,
            created_at=stamp,
        )
        for i, name in enumerate([large_name, "small"])
    ]
    client = CHClient()
    with query_tag("integration.code_reconciliation"):
        try:
            await client.connect()
            repo = CodeRepo(client)
            await repo.insert_rows("code_symbols", rows)
            if round_read:
                actual = (await repo.live_symbols_for_paths(org, namespace, ["main.py"], "scip"))[
                    "main.py"
                ]
            else:
                actual = await repo.live_symbols_for_path(org, namespace, "main.py", "scip")
            assert {
                str(row["symbol_id"]): {key: _value(value) for key, value in row.items()}
                for row in actual
            } == {
                row["symbol_id"]: {key: _value(value) for key, value in row.items()} for row in rows
            }
        finally:
            await client.disconnect()
