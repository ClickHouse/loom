"""Shared per-namespace cost-cap gate for background workers.

Workers that spend upstream tokens (conversation seeding, entity extraction)
check this before doing LLM/embed work so a runaway namespace can't blow its
budget. Reads the per-namespace `daily_cost_cap_usd` override, falling back to
`settings.default_daily_cost_cap_usd`; 0 on both tiers means uncapped.

Fail-open: any error in the budget read returns False (not over cap) so a
transient ClickHouse blip never silently halts a worker; a caller that spends in the
background (the conflict resolver) passes `fail_open=False`, and a failed read is over.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Literal

log = logging.getLogger(__name__)


async def namespaces_over_cap(
    pairs: Iterable[tuple[str, str]],
    *,
    operation: str = "",
    share: float = 1.0,
    fail_open: bool = True,
    ch: Any | None = None,
) -> dict[tuple[str, str], bool]:
    """`namespace_over_cap` for a set of namespaces in ONE statement: the
    24 h spend per namespace grouped over the set, its cap joined on. A
    namespace with no spend in the window is under any cap and is absent from
    the statement's rows, so only the namespaces that spent are read at all; a
    namespace without a `namespaces` row, or with cap 0, takes
    `settings.default_daily_cost_cap_usd`; cap 0 on both tiers is uncapped. The
    entity extractor asks once per pass for the namespaces of the rows it is
    about to extract, instead of two point reads per namespace. With `operation`, a
    namespace is also over once that operation's own spend reaches `share` of the cap
    (a worker held to its part of the cap). On a read error every pair reads
    `not fail_open`, as the single form does."""
    wanted = sorted({(str(o), str(n)) for o, n in pairs})
    if not wanted:
        return {}
    try:
        from server.core.ch import get_client

        client = ch or get_client()
        rows = await client.query_all(
            """
            WITH
                spend AS (
                    SELECT org, namespace, sum(upstream_cost_usd) AS spent,
                           sumIf(upstream_cost_usd, operation = {op:String}) AS own
                    FROM loom.agent_events
                    WHERE (org, namespace) IN {pairs:Array(Tuple(String, String))}
                      AND ts > now() - INTERVAL 1 DAY
                    GROUP BY org, namespace
                ),
                caps AS (
                    SELECT org, namespace, daily_cost_cap_usd AS cap
                    FROM loom.namespaces FINAL
                    WHERE (org, namespace) IN {pairs:Array(Tuple(String, String))}
                )
            SELECT s.org AS org, s.namespace AS namespace, s.spent AS spent, s.own AS own,
                   c.cap AS cap
            FROM spend AS s
            LEFT JOIN caps AS c ON s.org = c.org AND s.namespace = c.namespace
            """,
            params={"pairs": wanted, "op": operation},
        )
        out = dict.fromkeys(wanted, False)
        for r in rows:
            cap, _ = effective_cap(r.get("cap"))
            if cap <= 0:
                continue
            spent, own = float(r.get("spent") or 0.0), float(r.get("own") or 0.0)
            out[(str(r["org"]), str(r["namespace"]))] = _over(cap, spent, own, operation, share)
        return out
    except Exception:
        log.warning(
            "budget guard failed for %d namespaces (%s)",
            len(wanted),
            "proceeding without cap" if fail_open else "taken as over the cap",
            exc_info=True,
        )
        return dict.fromkeys(wanted, not fail_open)


def effective_cap(ns_cap: Any) -> tuple[float, Literal["namespace", "global", "uncapped"]]:
    """(cap, where it came from): the namespace's own cap when set above 0,
    else the server default; 0 on both tiers is uncapped."""
    from server.settings import settings

    own = float(ns_cap or 0.0)
    if own > 0:
        return own, "namespace"
    default = float(settings.default_daily_cost_cap_usd)
    return (default, "global") if default > 0 else (0.0, "uncapped")


def _over(cap: float, spent: float, own: float, operation: str, share: float) -> bool:
    """Over the whole cap, or, for `operation`, its own spend over its share of it."""
    return spent >= cap or (bool(operation) and own >= share * cap)


async def _cap_and_spend(
    org: str, namespace: str, operation: str = ""
) -> tuple[float, float, float]:
    """(cap, 24 h spend, `operation`'s part of it) for one namespace: the namespace's cap,
    else the server default; zeros when uncapped, without reading the spend. Raises on
    a read error; the public forms take that as under the cap, or over it with
    `fail_open=False`."""
    from server.core.ch import get_client

    ch = get_client()
    row = await ch.query_one(
        """
        SELECT daily_cost_cap_usd
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        """,
        params={"org": org, "ns": namespace},
    )
    cap, _ = effective_cap((row or {}).get("daily_cost_cap_usd"))
    if cap <= 0:
        return 0.0, 0.0, 0.0
    spent_row = await ch.query_one(
        """
        SELECT sum(upstream_cost_usd) AS spent,
               sumIf(upstream_cost_usd, operation = {op:String}) AS own
        FROM loom.agent_events
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND ts > now() - INTERVAL 1 DAY
        """,
        params={"org": org, "ns": namespace, "op": operation},
    )
    got = spent_row or {}
    return cap, float(got.get("spent") or 0.0), float(got.get("own") or 0.0)


async def namespace_over_cap(
    org: str, namespace: str, *, operation: str = "", share: float = 1.0, fail_open: bool = True
) -> bool:
    """True iff this namespace has hit its 24h upstream-cost ceiling; with
    `operation`, iff that operation's spend has reached `share` of it. A failed read is
    under the cap (a budget-read blip never halts a request), or over it with
    `fail_open=False` (a background spender)."""
    try:
        return await operation_over_share(org, namespace, operation=operation, share=share)
    except Exception:  # the caller chose which way a failed read goes
        log.warning(
            "budget guard failed for %s/%s (%s)",
            org,
            namespace,
            "proceeding without cap" if fail_open else "taken as over the cap",
            exc_info=True,
        )
        return not fail_open


async def operation_over_share(
    org: str, namespace: str, *, operation: str = "", share: float = 1.0
) -> bool:
    """`namespace_over_cap` with no choice made for a failed read: it raises, for a
    caller that answers an unreadable spend apart from a spent one."""
    cap, spent, own = await _cap_and_spend(org, namespace, operation)
    return cap > 0 and _over(cap, spent, own, operation, share)


async def namespace_budget_ratio(org: str, namespace: str) -> float:
    """24h upstream spend as a fraction of this namespace's cost cap.

    0.0 when no cap is configured (or on any read error — fail open, like
    `namespace_over_cap`), so a caller can treat >= 1.0 as capped and a
    value in between as budget PRESSURE. Same two point-reads as
    `namespace_over_cap`; callers that need both should call this once and
    derive the boolean, not pay the reads twice.
    """
    try:
        cap, spent, _ = await _cap_and_spend(org, namespace)
        return spent / cap if cap > 0 else 0.0
    except Exception:  # fail open: a budget-read blip never halts the caller
        log.warning(
            "budget: ratio check failed for %s/%s; treating as 0",
            org,
            namespace,
            exc_info=True,
        )
        return 0.0
