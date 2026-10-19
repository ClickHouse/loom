"""The by-key reads the revocation path makes, answered from a fake's rows of
loom.revoked_tokens, for the unit fakes that stand in for ClickHouse."""

from __future__ import annotations

import time
from typing import Any


def revocation_read(rows: list[dict[str, Any]], sql: str, params: Any) -> dict[str, Any] | None:
    """`next_version`'s read (on ClickHouse's clock, here this process's), and
    whether the key's newest row revokes it outright; None for anything else."""
    key = (params or {}).get("k0", (params or {}).get("j"))
    mine = [r for r in rows if r.get("jti") == key]
    newest = max(mine, key=lambda r: int(r.get("version") or 0), default=None)
    if "max(version)" in sql:
        top = int(newest.get("version") or 0) if newest else 0
        return {"v": max(time.time_ns(), top + 1)}
    if "issued_before IS NULL" in sql:
        return {"x": 1} if newest is not None and newest.get("issued_before") is None else None
    return None
