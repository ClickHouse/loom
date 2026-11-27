"""Whether anything indexes the configured repositories after a push.

Three things move an index: the `code_ingest` poller (`code_ingest_enabled`), an ingest
pass some process of the deployment recorded in the worker ledger, and a `code.refresh`
call, which leaves `last_refresh_at` on the entry. With the poller switched off here, an
entry with neither of the others is indexed by nothing.

This process keeps the count of such entries (`refresh`, on the code_repos refresh
timer), which the `code_repos_untriggered` health problem reads (`untriggered`) without
a ClickHouse read in the health probe. A web process that runs no worker answers like
any other: the ledger and the entry's state are the deployment's, not the process's.
"""

from __future__ import annotations

import logging
from typing import Any

from server.settings import settings

logger = logging.getLogger("loom.code_index.ingest_trigger")

# How far back an ingest pass in the worker ledger counts as a poller at work.
_LEDGER_DAYS = 14
_REFRESH_KEY = "last_refresh_at"

# Configured entries nothing indexes, as this process last read them. None until the
# first read.
_UNTRIGGERED: int | None = None


async def refresh(ch: Any | None = None) -> None:
    """Re-count the configured entries nothing indexes. With the poller on, code
    intelligence off or no entry configured the count is 0 and nothing is read; else
    two small reads. Until the registry is first read, which entries are configured is
    unknown, and the count stays as it was. Never raises: a failed read keeps the last
    count, so a ledger that cannot be read is never taken for an empty one."""
    global _UNTRIGGERED
    from server.code.app.index import repo_registry
    from server.code.domain.index.repos import configured_repos

    if settings.code_ingest_enabled or not settings.code_intel_enabled:
        _UNTRIGGERED = 0
        return
    if not repo_registry.registry_read():
        return
    try:
        entries = {(c.org, c.namespace) for c in configured_repos()}
        _UNTRIGGERED = len(entries - await _triggered(entries, ch)) if entries else 0
    except Exception as e:  # noqa: BLE001 — the health signal keeps its last reading
        logger.warning("ingest trigger: untriggered repositories not re-counted: %s", e)


async def _triggered(entries: set[tuple[str, str]], ch: Any | None) -> set[tuple[str, str]]:
    """The entries of `entries` something indexes: all of them when the ledger holds an
    ingest pass, else the ones with a `code.refresh` on record."""
    from server.code.store.index import CodeRepo
    from server.workers import status_store

    passes = await status_store.pass_summaries("code_ingest", _LEDGER_DAYS, ch=ch)
    if any(str(r.get("worker")) == "code_ingest" for r in passes):
        return entries
    rows = await CodeRepo(ch).state_rows_with_prefix(_REFRESH_KEY)
    return {
        (str(r["org"]), str(r["namespace"]))
        for r in rows
        if str(r["state_key"]) == _REFRESH_KEY and r.get("state_value")
    }


def untriggered() -> int | None:
    """The count `refresh` last made; None until this process first read it."""
    return _UNTRIGGERED


def reset_for_tests() -> None:
    global _UNTRIGGERED
    _UNTRIGGERED = None
