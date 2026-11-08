"""Repo migrations vs the service's ledger, for `/v1/health`.

`schema.pending` — migration files in this build's `schemas/migrations/` that
`loom._migrations` has not recorded: the service is behind the code (an
isolated server never applies them; the deployment applies them at boot, in
the leader step). `schema.ledger_only` — ledger entries no file in this build
carries: the code is behind the service, or a file was renamed.

One `SELECT filename FROM loom._migrations FINAL` per process per
`schema_status_ttl_s`; the health probe runs every few seconds and the answer
changes at a deploy. Never raises: an unreadable ledger reports `error`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from server.core.ch_schema_guard import schema_guard_reset
from server.core.errors import error_kind
from server.core.paths import MIGRATIONS_DIR
from server.core.schema.journal import (
    baseline_map,
    classify_ledger,
    created_objects,
    sorted_migrations,
)
from server.settings import settings

log = logging.getLogger(__name__)

_cache: dict[str, Any] = {"at": 0.0, "value": None}


def repo_migrations(mig_dir: Path | None = None) -> list[str]:
    """Filenames this build carries, in apply order."""
    return [p.name for p in sorted_migrations((mig_dir or MIGRATIONS_DIR).glob("*.sql"))]


def repo_baselines(mig_dir: Path | None = None) -> dict[str, int]:
    """`{baseline file: N}` for this build's baselines."""
    return baseline_map((mig_dir or MIGRATIONS_DIR).glob("*.sql"))


def compare(
    repo: list[str], ledger: list[str], covers: dict[str, int] | None = None
) -> dict[str, Any]:
    """Pure: the two lists → {pending, ledger_only, applied, superseded}. A
    baseline the ledger already covers is not pending: it is recorded
    without running, and its tables exist."""
    classes = classify_ledger(repo, ledger, covers or {})
    return {
        "pending": sorted(classes.pending),
        "ledger_only": sorted(classes.ledger_only),
        "applied": len(set(ledger) & set(repo)),
        "superseded": len(classes.superseded),
    }


async def schema_status(*, ttl_s: float | None = None, now: float | None = None) -> dict[str, Any]:
    """{pending, ledger_only, applied} (+ `error` when the ledger is
    unreadable), cached per process for `ttl_s`."""
    if ttl_s is None:
        ttl_s = settings.schema_status_ttl_s
    t = time.monotonic() if now is None else now
    cached = _cache["value"]
    if cached is not None and t - float(_cache["at"]) < ttl_s:
        return dict(cached)
    try:
        from server.core.ch import get_client

        rows = await get_client().query_all("SELECT filename FROM loom._migrations FINAL")
        ledger = [str(r.get("filename") or "").rstrip("\x00") for r in rows]
        out = compare(repo_migrations(), ledger, repo_baselines())
    except Exception as e:  # noqa: BLE001 — health never fails on this read
        # `/v1/health` shows this without a token: the error's kind; the text is logged.
        log.warning("schema status: the migrations ledger read failed: %s: %s", type(e).__name__, e)
        out = {
            "pending": None,
            "ledger_only": None,
            "applied": None,
            "error": error_kind(e),
        }
    _cache["at"], _cache["value"] = t, out
    return dict(out)


# What a pending migration CREATES. A read of one of these
# tables on a service that has not applied the file raises code 60 inside a
# leg; the client refuses the statement up front instead (server/core/ch.py).
_created_cache: dict[str, tuple[str, ...]] = {}


def created_tables(filename: str, mig_dir: Path | None = None) -> tuple[str, ...]:
    """The table names one migration file creates, bare for `loom` and
    qualified for another database (parsed once per process); () for a file
    that cannot be read."""
    hit = _created_cache.get(filename)
    if hit is not None:
        return hit
    try:
        text = ((mig_dir or MIGRATIONS_DIR) / filename).read_text(encoding="utf-8")
        names = created_objects(text)
    except Exception:  # noqa: BLE001 — an unreadable file guards nothing
        names = ()
    _created_cache[filename] = names
    return names


def pending_created_tables(
    pending: list[str] | None, mig_dir: Path | None = None
) -> dict[str, str]:
    """`{table: migration filename}` for every table a pending migration
    creates — the set the read-side guard refuses. Empty
    for no pending files or an unreadable ledger (`pending is None`)."""
    out: dict[str, str] = {}
    for fname in pending or []:
        for table in created_tables(str(fname), mig_dir):
            out.setdefault(table, str(fname))
    return out


def reset_cache() -> None:
    """Forget this cache, the parsed files, and the client's guard set
    derived from them (after boot migrations, and in tests)."""
    _cache["at"], _cache["value"] = 0.0, None
    _created_cache.clear()
    schema_guard_reset()
