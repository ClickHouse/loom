"""Retention reconcile worker.

Per-namespace retention lives in `loom.namespaces`; the table TTLs that
enforce it are a `multiIf` over every namespace with `retention_days`, so
every registration with a retention changes the clause. Re-applying the TTL
inline in the register handler would mean one ALTER per managed table per
registration, and bench harnesses register a namespace per task — hundreds of
MODIFY TTL mutations an hour, each a full rewrite of the table.

This worker is the only steady-state writer of table TTLs: it runs in the
supervisor (leader) process on a timer, and `retention_overrides.apply`
already skips a table whose TTL is unchanged, so a quiet interval costs one
`system.tables` read and two format calls per managed table. A policy
change made through `namespace.register` is on the tables within
`retention_reconcile_interval_seconds`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.settings import settings
from server.storage import retention_overrides
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    alters_issued: int


async def run_pass() -> ReconcileResult:
    """One pass: recompose every managed table's TTL from settings plus the
    namespace rows and alter the tables whose clause differs. The
    supervisor only runs in the lease holder, so `leader=True` here."""
    with record_pass("retention_reconcile") as rec:
        issued = await retention_overrides.apply(settings, leader=True)
        rec.outcome = {"alters_issued": issued}
    if issued:
        log.info("retention_reconcile: %d table TTL(s) updated", issued)
    return ReconcileResult(alters_issued=issued)
