"""Refresh proved probe/upstream addresses without claiming fleet-wide cache agreement."""

from __future__ import annotations

import json
from typing import Any

from server.cli import cell_control, cell_update_probe
from server.router.directory import CONTROL_FIELDS
from server.router.routing import cell_address


def normalise(value: str) -> str:
    address = cell_address(value)
    if not address:
        raise cell_control._refused("probe address is an HTTP(S) host-only URL")
    return address


async def refresh(run: Any) -> dict[str, Any]:
    if run.row["is_first"]:
        if run.args.probe_url:
            raise cell_control._refused(
                "the first cell refreshes router-configured addresses without --probe-url"
            )
        proposal = await run.call("/addresses")
        if json.dumps(cell_control.preconditions(proposal), sort_keys=True) != json.dumps(
            cell_control.preconditions(run.row), sort_keys=True
        ):
            raise cell_control._refused(
                "directory ownership changed while reading router addresses"
            )
        probe_url = normalise(proposal.get("router_upstream_probe_url") or "")
        upstream_url = normalise(proposal.get("router_upstream_url") or "")
    else:
        if not run.args.probe_url:
            raise cell_control._refused("a later cell probe refresh requires --probe-url")
        probe_url, upstream_url = normalise(run.args.probe_url), run.row["upstream_url"]
    answer = await cell_update_probe.post(
        run, "/addresses", {"probe_url": probe_url, "upstream_url": upstream_url}
    )
    if (
        answer["mutation_generation"] != run.row["mutation_generation"]
        or any(
            json.dumps(answer[name], sort_keys=True) != json.dumps(run.row[name], sort_keys=True)
            for name in CONTROL_FIELDS
            if name not in ("probe_url", "upstream_url")
        )
        or (answer["probe_url"], answer["upstream_url"]) != (probe_url, upstream_url)
    ):
        raise cell_control._refused(
            "address acknowledgement differs from the proved addresses; outcome unknown"
        )
    return {
        "cell": run.args.cell,
        "probe_url": probe_url,
        "upstream_url": upstream_url,
        "router_refresh_required": bool(run.row["is_first"]),
        "next_step": (
            "Keep old endpoints reachable until every router refreshes its routing "
            "table or restarts after this directory update; this command cannot "
            "prove all-router agreement"
        ),
    }
