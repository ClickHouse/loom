"""Whether a source meets the tracker record contract, read through its own
credential: the schema described once per process per TTL
(`server/tracker/domain/contract.py` holds the contract and the diff), the
admission gate every read passes, and the boot-time warm-up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.tracker.domain.contract import (
    ALIASES,
    OPTIONAL,
    REQUIRED,
    ContractFailed,
    ContractReport,
    Described,
    diff,
    diff_mapped,
)
from server.tracker.store.source import contract_reports as reports

log = logging.getLogger("loom.tracker.contract")


def _tables_of_interest() -> list[str]:
    return sorted(set(REQUIRED) | set(OPTIONAL) | set(ALIASES))


async def describe(
    credential: str, database: str, *, budget_s: float = 5.0, query: Any | None = None
) -> Described:
    """table → column → type for the contract's tables, through the source's
    credential: `system.columns` in one statement, and `DESCRIBE` per
    required table when the catalogue is not readable by that user. `query`
    (`async (sql, params) -> rows`) replaces the configured client — the
    onboarding probe describes a source before it is configured."""
    from server.tracker.store.source import qa_intel

    async def _q(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if query is not None:
            return list(await query(sql, params))
        return await qa_intel.query_rows(
            sql, params, credential=credential, max_execution_s=budget_s
        )

    tables = _tables_of_interest()
    rows = await asyncio.wait_for(
        _q(
            "SELECT table, name, type FROM system.columns"
            " WHERE database = {db:String} AND table IN {tables:Array(String)}",
            {"db": database, "tables": tables},
        ),
        timeout=budget_s,
    )
    out: Described = {}
    for r in rows:
        out.setdefault(str(r.get("table") or ""), {})[str(r.get("name") or "")] = str(
            r.get("type") or ""
        )
    if any(t in out for t in REQUIRED):
        return out
    for table in REQUIRED:  # the catalogue is hidden from this user: ask each table
        try:
            desc = await asyncio.wait_for(
                _q(f"DESCRIBE TABLE {database}.{table}"), timeout=budget_s
            )
        except Exception as e:  # noqa: BLE001 — an unknown table is "missing"
            log.info("contract[%s] DESCRIBE %s.%s: %s", credential, database, table, e)
            continue
        out[table] = {str(r.get("name") or ""): str(r.get("type") or "") for r in desc}
    return out


async def describe_mapped(
    credential: str,
    database: str,
    mapping: Any,
    *,
    budget_s: float = 5.0,
    query: Any | None = None,
) -> dict[str, dict[str, str]]:
    """`db.table` → column → type for the team's tables a
    mapping names, in one `system.columns` read through the credential."""
    from server.tracker.store.source import qa_intel

    async def _q(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if query is not None:
            return list(await query(sql, params))
        return await qa_intel.query_rows(
            sql, params, credential=credential, max_execution_s=budget_s
        )

    refs = sorted(
        {t.source if "." in t.source else f"{database}.{t.source}" for t in mapping.tables.values()}
    )
    rows = await asyncio.wait_for(
        _q(
            "SELECT database, table, name, type FROM system.columns"
            " WHERE concat(database, '.', table) IN {refs:Array(String)}",
            {"refs": refs},
        ),
        timeout=budget_s,
    )
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        key = f"{r.get('database') or ''}.{r.get('table') or ''}"
        out.setdefault(key, {})[str(r.get("name") or "")] = str(r.get("type") or "")
    return out


# Every deployment gates a source on its contract. Unit tests fake the upstream
# one statement at a time, so their conftest turns the gate off here.
_enforce = True


def enforced() -> bool:
    return _enforce


async def check(credential: str, database: str) -> ContractReport:
    """Read the schema and diff it; cache the report for the TTL (a failed
    one for a minute). A mapped tracker is read through its mapping — the tables it
    names, not loom's logical ones."""
    from server.tracker.domain import mapping as _mapping
    from server.tracker.domain import sources as _sources

    try:
        mp = _sources.mapping_for(credential)
    except _mapping.MappingError as e:
        rep = ContractReport(
            source=credential,
            database=database,
            checked_at=time.monotonic(),
            mode="mapping",
            ok=False,
            mapping_errors=[f"mapping: {e}"],
        )
        reports.remember(credential, rep)
        return rep
    if mp is not None:
        live = await describe_mapped(credential, database, mp)
        rep = diff_mapped(credential, database, mp, live)
    else:
        described = await describe(credential, database)
        rep = diff(credential, database, described, _sources.column_renames(credential))
    reports.remember(credential, rep)
    if not rep.ok:
        log.error(
            "qa source %r (%s) fails the record contract: %s", credential, database, rep.summary()
        )
    return rep


async def ensure(cfg: Any) -> ContractReport | None:
    """The gate `TrackerSource._admit` runs: a cached report, else a fresh check.
    Raises `ContractFailed` when the source does not meet the contract; None
    when enforcement is off."""
    if cfg is None:
        return None
    credential = str(getattr(cfg, "credential", "") or "")
    from server.tracker.domain import sources as _sources

    ep = _sources.endpoint(credential)
    # A mapped tracker's statements cannot run without the
    # report's projections, so it is checked whether or not enforcement is on
    if not enforced() and not (ep is not None and ep.mapping_json):
        return None
    rep = reports.cached(credential)
    if rep is None:
        rep = await check(credential, str(getattr(cfg, "database", "") or ""))
    if not rep.ok:
        raise ContractFailed(f"contract: {rep.summary()}")
    return rep


def provides(cfg: Any, table: str) -> bool:
    """Whether the namespace's source provides `table`: True for a required
    table, for every table when enforcement is off or no report is cached
    yet, else what the report says."""
    if table in REQUIRED or not enforced() or cfg is None:
        return True
    rep = reports.cached(str(getattr(cfg, "credential", "") or ""))
    return True if rep is None else rep.provides(table)


async def warm_at_boot(*, timeout_s: float = 20.0) -> dict[str, str]:
    """Open each configured tracker's client and cache its
    contract report before the first request needs them. Without it the
    first read builds the client and runs `check` inline inside its 900 ms
    step budget, so `code.backport_gap` answers `unknown` until the report is
    cached. Outcome per source; never raises."""
    from server.tracker.domain import sources as _sources
    from server.tracker.store.source import qa_intel

    out: dict[str, str] = {}
    for name, ep in _sources.endpoints().items():
        if not ep.configured or not _sources.credential_configured(name):
            continue
        try:
            await asyncio.wait_for(qa_intel._get_client(name), timeout=timeout_s)
            if enforced() or ep.mapping_json:
                rep = await asyncio.wait_for(check(name, ep.database), timeout=timeout_s)
                out[name] = "ok" if rep.ok else "contract_failed"
            else:
                out[name] = "client"
        except Exception as e:  # noqa: BLE001 — a warm-up failure leaves the lazy path
            out[name] = f"failed: {type(e).__name__}"
    if out:
        log.info("tracker warm-up at boot: %s", out)
    return out


__all__ = [
    "check",
    "describe",
    "describe_mapped",
    "enforced",
    "ensure",
    "provides",
    "warm_at_boot",
]
