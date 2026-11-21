"""Doctor's opt-in capacity reader, using its declared stores and operator route."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from server.cli import cell as cell_cli
from server.cli import doctor_cell
from server.cli._ch import connect_declared, disconnect, reads
from server.cli.doctor import FAIL, PASS, WARN
from server.core import cell, pg, pg_profile
from server.core.ch import get_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import capacity
from server.settings import settings


def _placement() -> tuple[str, str, str]:
    """Use the existing operator read; no cell credential or quota mutation."""
    if not settings.cell or not cell.NAME.fullmatch(settings.cell):
        return "capacity_placement", WARN, "unavailable: no configured cell"
    router = cell_cli.operator_router(argparse.Namespace(router=None))
    if isinstance(router, str):
        return "capacity_placement", WARN, "unavailable: operator router is not configured"
    try:
        row = cell_cli._read_row(router, settings.cell)
    except Exception:  # noqa: BLE001 — an unread directory is a warning, never a pass
        row = "unavailable"
    if not isinstance(row, dict):
        return "capacity_placement", WARN, "unavailable: directory read failed"
    if row.get("cell") != settings.cell or row.get("deployment_id") != cell.deployment_id():
        return "capacity_placement", WARN, "unavailable: directory identity does not match"
    if "placements_max" not in row:
        return "capacity_placement", WARN, "missing quota: directory answer contains no limit"
    placed, maximum, closed = row.get("placed_orgs"), row["placements_max"], row.get("closed")
    if (
        type(placed) is not int
        or not 0 <= placed <= cell.PLACEMENTS_MAX
        or type(closed) is not bool
    ):
        return "capacity_placement", WARN, "unavailable: invalid directory limits"
    if maximum is not None and (
        type(maximum) is not int or not 0 <= maximum <= cell.PLACEMENTS_MAX
    ):
        return "capacity_placement", WARN, "unavailable: invalid directory quota"
    quota = "unlimited (no configured quota)" if maximum is None else str(maximum)
    return (
        "capacity_placement",
        PASS,
        (
            f"placed_orgs={placed}, placements_max={quota}, closed={closed}; "
            f"operator read completed at {datetime.now(UTC).isoformat()}"
        ),
    )


async def _observe(org: str | None, namespace: str | None) -> dict[str, object]:
    from server.auth import tenancy

    ch = get_client()
    try:
        await asyncio.wait_for(
            connect_declared(
                ch, org_free=reads("loom doctor reads capacity"), check_authority=True
            ),
            timeout=10,
        )
        # Reuse doctor's authoritative deployment comparison before PG or scope reads.
        if pg.enabled():
            before = await asyncio.wait_for(doctor_cell._deployment_check(ch), timeout=10)
            if before:
                return {"scope": {"status": "unknown", "reason": "deployment has not booted"}}
        if settings.cell:
            async with asyncio.timeout(10):
                cell.set_deployment_id(await pg_profile.deployment_id())
                await cell.refresh()
            if cell.cli_gate_reason():
                raise ValueError("configured stores do not match the cell")

        async def authorized_scope() -> Any:
            if org and not await tenancy.is_held(org):
                raise PermissionError("scope unavailable")
            return WHOLE_NAMESPACE

        return await capacity.observe(org, namespace, authorize=authorized_scope, ch=ch)
    finally:
        first = sys.exception()
        try:
            await disconnect(ch)
        except BaseException:
            if first is None:
                raise


def capacity_checks(org: str | None, namespace: str | None) -> list[tuple[str, str, str]]:
    """One requested scope at most; failure is reported without raw database errors."""
    import json

    if not capacity.selection_valid(org, namespace):
        return [("capacity_scope", FAIL, "select exactly one named org and namespace")]
    try:
        answer = asyncio.run(_observe(org, namespace))
    except Exception:  # noqa: BLE001 — unread or mismatched stores are not capacity evidence
        return [("capacity", WARN, "unavailable: stores or deployment could not be verified")]
    out = [_placement()]
    for name, value in answer.items():
        grade = WARN if isinstance(value, dict) and value.get("status") == "unknown" else PASS
        if name == "scope" and isinstance(value, dict):
            counts = (value.get("live_heads"), value.get("vector_physical_rows"))
            if any(
                isinstance(count, dict) and count.get("status") == "unknown" for count in counts
            ):
                grade = WARN
        out.append((f"capacity_{name}", grade, json.dumps(value, sort_keys=True)))
    return out
