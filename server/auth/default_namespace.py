"""Auth-disabled default-namespace bootstrap.

With auth off there is no JWT and no grant to derive a namespace from, so
every caller shares one configured namespace (`LOOM_DEFAULT_NAMESPACE`,
default `local`). This module provisions that namespace the same way
self-signup would — a `loom.namespaces` row plus an admin grant for the
single-user subject `"you"` (via `provision_account`, so the row shape
can't drift from the real creation paths). The grant is what makes
`namespace.list_mine` actually return the row, and what keeps the
namespace administrable after auth is later enabled.

Runs at most once per process — the row can only need creating once, and
the `/me` dashboard would otherwise re-run a blocking `FINAL` lookup on
every page load. Mirrors `ensure_personal_namespace_cached`: best-effort,
errors are logged and the cache is *not* set on failure so the next call
retries. An `asyncio.Lock` keeps N concurrent first requests from each
probing and inserting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

_bootstrapped = False
_lock = asyncio.Lock()

# The shared single-user subject: the MCP transport, `/me`'s user_state
# key and this bootstrap must agree on one name or the grant is invisible.
_SINGLE_USER_SUBJECT = "you"

# Cap on how many stray namespaces the rename warning names individually.
_WARN_SAMPLE = 20


def _description(namespace: str) -> str:
    """Row description, phrased for the name actually in use."""
    head = (
        "Local single-user data."
        if namespace == "local"
        else f"Single-user data for {namespace!r} (LOOM_DEFAULT_NAMESPACE)."
    )
    return (
        f"{head} Persists across sessions and restarts. Promote to a named "
        "account any time by enabling auth."
    )


async def _warn_on_stranded_namespaces(ch: Any, org: str, namespace: str) -> None:
    """Shout if auth-off data lives outside the configured default.

    Only called when this boot actually (re)provisioned the row — i.e. on
    first boot or right after a `LOOM_DEFAULT_NAMESPACE` change, which is
    exactly when an operator is watching the log. A healthy deployment that
    deliberately holds extra namespaces (SDK `namespace.register` works fine
    auth-off) doesn't re-hear it on every boot × replica. `personal-*` is
    excluded: those are per-subject namespaces from auth-on sessions, not
    strays.
    """
    row = await ch.query_one(
        f"""
        SELECT count() AS n, groupArray({_WARN_SAMPLE})(namespace) AS sample
        FROM loom.namespaces FINAL
        WHERE org = {{org:String}}
          AND namespace != {{ns:String}}
          AND namespace NOT LIKE 'personal-%'
          AND active = true
        """,
        params={"org": org, "ns": namespace},
    )
    n = int(row.get("n") or 0) if row else 0
    if not n:
        return
    sample = [str(s) for s in (row.get("sample") or [])] if row else []
    shown = ", ".join(sorted(sample))
    if n > len(sample):
        shown += f", … (+{n - len(sample)} more)"
    log.warning(
        "auth is disabled and LOOM_DEFAULT_NAMESPACE=%s, but org %r has %d "
        "other namespace(s): %s. The dashboard can only reach %s — data in the "
        "others stays queryable via SDK/MCP but is invisible in /me, and a "
        "later auth-enabled migration sees it as ownerless. Point "
        "LOOM_DEFAULT_NAMESPACE at the one you meant, or enable auth to reach "
        "them all.",
        namespace,
        org,
        n,
        shown,
        namespace,
    )


async def ensure_default_namespace_cached() -> None:
    """Idempotently ensure the auth-off default namespace has an active row.

    Cheap after the first successful call (one module-level bool check), so
    request paths can call it unconditionally.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    async with _lock:
        if _bootstrapped:
            return

        inserted = False
        try:
            from server.auth.self_signup import provision_account
            from server.core.ch import get_client
            from server.settings import settings

            namespace = settings.default_namespace
            org = settings.self_signup_org
            ch = get_client()
            existing = await ch.query_one(
                "SELECT active FROM loom.namespaces FINAL "
                "WHERE org = {org:String} AND namespace = {ns:String} LIMIT 1",
                params={"org": org, "ns": namespace},
            )
            # Missing OR soft-deleted (`namespace.delete` writes an
            # active=false row): (re)provision. The insert is a new RMT
            # version, so reactivation is the same write as creation.
            if existing is None or not existing.get("active"):
                await provision_account(
                    org=org,
                    namespace=namespace,
                    subject=_SINGLE_USER_SUBJECT,
                    contact_email="",
                    description=_description(namespace),
                )
                inserted = True
                log.info(
                    "bootstrapped auth-disabled namespace %s/%s%s",
                    org,
                    namespace,
                    " (reactivated)" if existing is not None else "",
                )
            # The bootstrap contract ends here: cache success BEFORE the
            # diagnostic scan so a scan failure can't put every subsequent
            # request into a probe-retry storm with a misleading traceback.
            _bootstrapped = True
        except Exception:
            log.warning("auth-disabled namespace row bootstrap failed", exc_info=True)
            return

        if inserted:
            try:
                await _warn_on_stranded_namespaces(ch, org, namespace)
            except Exception:
                log.debug(
                    "stranded-namespace scan failed (diagnostic only)",
                    exc_info=True,
                )
