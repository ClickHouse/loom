"""A code namespace being erased, on best effort: writers skip it when their work on it
starts, and its purge refuses, retryably, while work begun before the erasure was
recorded may still write it. `verify.residue` finds what this misses."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from server.core import leases
from server.core.errors import OverloadedError

_At = Callable[[], Awaitable[datetime]]
log = logging.getLogger(__name__)
# Their passes hold the entry lock, which names the namespace they are on.
_ENTRY_LOCKED = frozenset({"code_scip_full", "code_scip_delta"})


async def being_erased(org: str, namespace: str) -> bool:
    """True too when the ledger cannot be read: no work starts that the purge may miss."""
    from server.erasure.app import ledger_reads
    from server.erasure.domain.scopes import may_cover

    since, margin = int(time.time()), ledger_reads.LEDGER_CLOCK_MARGIN_S
    try:
        rows = await ledger_reads.unfinished_reaching(
            org, namespace, "", since, margin, skip_abandoned=True
        )
    except Exception as e:  # noqa: BLE001 — the pass goes on with the other namespaces
        log.warning("%s/%s: erasure ledger not read, skipped: %s", org, namespace, type(e).__name__)
        return True
    return any(may_cover(str(r["scope_type"]), str(r["scope_value"]), namespace) for r in rows)


async def _pass_begun_before(recorded: datetime) -> str:
    """A code worker whose newest pass row finished an interval (none after a failure) before
    `recorded`, so its next pass may have begun by then, and whose deadline is not past."""
    from server.workers import status_store

    rows = [r for r in await status_store.latest_rows() if r.get("trigger") != "manual"]
    newest = {r["worker"]: r for r in sorted(rows, key=lambda r: r.get("pass_finished") or "")}
    now = datetime.now(UTC)
    for name, row in sorted(newest.items()):
        if not name.startswith("code_") or name in _ENTRY_LOCKED or not row["pass_finished"]:
            continue
        rest = row.get("interval_s") if row.get("status") in ("ok", "idle", "partial") else 0
        began_by = datetime.fromisoformat(row["pass_finished"]) + timedelta(seconds=rest or 0)
        if began_by <= recorded and now < recorded + timedelta(seconds=row["deadline_s"] or 0):
            return str(name)
    return ""


async def _holder(org: str, namespaces: Sequence[str], recorded: _At) -> str:
    """What may still write an indexed one of `namespaces`, '' when nothing is known to."""
    from server.code.app.index.entry_lock import entry_lease_name
    from server.code.app.ingest import refresh_lease_name
    from server.core.ch import get_client

    rows = await get_client().query_all(
        "SELECT DISTINCT namespace FROM loom.code_index_state "
        "WHERE org = {org:String} AND namespace IN {ns:Array(String)}",
        params={"org": org, "ns": list(namespaces)},
    )
    for ns in sorted(str(r["namespace"]) for r in rows):
        if await leases.has_live_holder(entry_lease_name(org, ns)):
            return f"a compiler-tier pass holds the entry lock of {ns!r}"
        if await leases.has_live_holder(refresh_lease_name(org, ns)):
            return f"a code.refresh pass is indexing {ns!r}"
    if rows and (worker := await _pass_begun_before(await recorded())):
        return f"a {worker} pass that began before the erasure may still be writing"
    return ""


async def refuse_while_in_use(org: str, namespaces: Sequence[str], recorded: _At) -> None:
    """OVERLOADED while work begun before the erasure was `recorded()` may still write."""
    if held := await _holder(org, namespaces, recorded):
        raise OverloadedError(
            f"{held}; the code index is purged once that ends: the recovery worker retries "
            "the erasure, or repeat it",
            retry_after_s=60,
        )
