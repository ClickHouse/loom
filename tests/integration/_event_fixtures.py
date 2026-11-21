"""Place native event fixtures in their ordinary or private physical stream."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any

from server.core.ch import CHClient

_MIRROR = re.compile(r"acl-[0-9a-f]{24}\Z")


async def insert_event_rows(
    client: CHClient,
    table: str,
    rows: Sequence[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    *,
    required_columns: Collection[str] = (),
) -> None:
    assert table.endswith(".agent_events"), "An event fixture must name its ordinary stream"
    ordinary, private = [], []
    for row in rows:
        target = (
            private
            if row.get("mirror_marked", 0) or _MIRROR.fullmatch(row["namespace"])
            else ordinary
        )
        target.append(row)
    for target, batch in ((table, ordinary), (table + "_private", private)):
        if batch:
            await client.insert(target, batch, settings, required_columns=required_columns)
