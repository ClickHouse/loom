"""Shared namespace and named-user cost-cap gates.

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
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from server.core.event_source import ALL_EVENTS

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
            f"""
            WITH
                spend AS (
                    SELECT org, namespace, sum(upstream_cost_usd) AS spent,
                           sumIf(upstream_cost_usd, operation = {{op:String}}) AS own
                    FROM {ALL_EVENTS}
                    WHERE (org, namespace) IN {{pairs:Array(Tuple(String, String))}}
                      AND ts > now() - INTERVAL 1 DAY
                    GROUP BY org, namespace
                ),
                caps AS (
                    SELECT org, namespace, daily_cost_cap_usd AS cap
                    FROM loom.namespaces FINAL
                    WHERE (org, namespace) IN {{pairs:Array(Tuple(String, String))}}
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


@dataclass(frozen=True)
class BudgetSnapshot:
    cap: float
    cap_source: Literal["namespace", "global", "uncapped"]
    user_cap: float
    user_cap_source: Literal["namespace_user", "global_user", "uncapped"]
    spent: float = 0.0
    own: float = 0.0
    user_spent: float = 0.0
    user_own: float = 0.0
    inbox: str = ""

    def ratio(self, user_id: str) -> float:
        return max(
            self.spent / self.cap if self.cap > 0 else 0.0,
            self.user_spent / self.user_cap if user_id and self.user_cap > 0 else 0.0,
        )

    def over(self, user_id: str, operation: str, share: float) -> bool:
        return (self.cap > 0 and _over(self.cap, self.spent, self.own, operation, share)) or (
            bool(user_id)
            and self.user_cap > 0
            and _over(self.user_cap, self.user_spent, self.user_own, operation, share)
        )


def effective_user_cap(
    ns_cap: Any,
) -> tuple[float, Literal["namespace_user", "global_user", "uncapped"]]:
    from server.settings import settings

    own = float(ns_cap or 0.0)
    if own > 0:
        return own, "namespace_user"
    default = float(settings.default_daily_user_cost_cap_usd)
    return (default, "global_user") if default > 0 else (0.0, "uncapped")


def named_user(user_id: str | None) -> str:
    from server.core.naming import named

    return named(user_id) or ""


async def budget_snapshot(
    org: str,
    namespace: str,
    *,
    operation: str = "",
    user_id: str = "",
    read_uncapped: bool = False,
    ch: Any | None = None,
) -> BudgetSnapshot:
    """One policy read and one rolling spend read, shared by status and both guards."""
    from server.core.ch import get_client

    observed_at = time.time()
    ch = ch or get_client()
    row = (
        await ch.query_one(
            "SELECT daily_cost_cap_usd, daily_user_cost_cap_usd, budget_alert_inbox "
            "FROM loom.namespaces FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} LIMIT 1",
            params={"org": org, "ns": namespace},
        )
        or {}
    )
    cap, source = effective_cap(row.get("daily_cost_cap_usd"))
    user_cap, user_source = effective_user_cap(row.get("daily_user_cost_cap_usd"))
    uid = named_user(user_id)
    values: dict[str, Any] = {}
    if read_uncapped or cap > 0 or (uid and user_cap > 0):
        values = (
            await ch.query_one(
                "SELECT sum(upstream_cost_usd) AS spent, "
                "sumIf(upstream_cost_usd, operation = {op:String}) AS own, "
                "sumIf(upstream_cost_usd, user_id = {uid:String}) AS user_spent, "
                "sumIf(upstream_cost_usd, user_id = {uid:String} "
                "AND operation = {op:String}) AS user_own "
                f"FROM {ALL_EVENTS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
                "AND ts > now() - INTERVAL 1 DAY",
                params={"org": org, "ns": namespace, "op": operation, "uid": uid},
            )
            or {}
        )
    snapshot = BudgetSnapshot(
        cap=cap,
        cap_source=source,
        user_cap=user_cap,
        user_cap_source=user_source,
        spent=float(values.get("spent") or 0.0),
        own=float(values.get("own") or 0.0),
        user_spent=float(values.get("user_spent") or 0.0),
        user_own=float(values.get("user_own") or 0.0),
        inbox=str(row.get("budget_alert_inbox") or ""),
    )

    if snapshot.inbox:
        from server.observability.budget_notifications import BudgetWarning, notify
        from server.settings import settings

        share = float(settings.cost_budget_alert_share)
        for who, limit, spent in (
            ("", cap, snapshot.spent),
            (uid, user_cap if uid else 0.0, snapshot.user_spent),
        ):
            if limit > 0 and spent >= share * limit:
                notify(
                    BudgetWarning(
                        org, namespace, snapshot.inbox, who, limit, spent, share, observed_at
                    )
                )
    return snapshot


async def namespace_over_cap(
    org: str,
    namespace: str,
    *,
    operation: str = "",
    share: float = 1.0,
    fail_open: bool = True,
    user_id: str = "",
) -> bool:
    """True when the namespace or supplied named user has hit its 24h ceiling; with
    `operation`, iff that operation's spend has reached `share` of it. A failed read is
    under the cap (a budget-read blip never halts a request), or over it with
    `fail_open=False` (a background spender)."""
    try:
        return await operation_over_share(
            org, namespace, operation=operation, share=share, user_id=user_id
        )
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
    org: str, namespace: str, *, operation: str = "", share: float = 1.0, user_id: str = ""
) -> bool:
    """`namespace_over_cap` with no choice made for a failed read: it raises, for a
    caller that answers an unreadable spend apart from a spent one."""
    snapshot = await budget_snapshot(org, namespace, operation=operation, user_id=user_id)
    return snapshot.over(named_user(user_id), operation, share)


async def namespace_budget_ratio(org: str, namespace: str, *, user_id: str = "") -> float:
    """Largest 24h spend fraction of the namespace and supplied named user's caps.

    0.0 when no cap is configured (or on any read error — fail open, like
    `namespace_over_cap`), so a caller can treat >= 1.0 as capped and a
    value in between as budget PRESSURE. Same two point-reads as
    `namespace_over_cap`; callers that need both should call this once and
    derive the boolean, not pay the reads twice.
    """
    try:
        snapshot = await budget_snapshot(org, namespace, user_id=user_id)
        return snapshot.ratio(named_user(user_id))
    except Exception:  # fail open: a budget-read blip never halts the caller
        log.warning(
            "budget: ratio check failed for %s/%s; treating as 0",
            org,
            namespace,
            exc_info=True,
        )
        return 0.0
