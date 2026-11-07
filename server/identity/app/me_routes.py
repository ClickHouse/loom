"""The caller API: `/v1/me/*`, the JSON endpoints about the authenticated
caller that the dashboard calls. Per-user state and the sharing summary here,
the caller's own tokens in `me_service_accounts.py`. `attach` mounts all of it;
the dashboard page itself is `dashboard_routes.py`.

These endpoints authenticate through `me_callers`, not the operation
registry's role gate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from server.core.datetimes import naive_utc_now_seconds
from server.core.grant_sql import LIVE_GRANT
from server.identity.app import me_callers, me_service_accounts
from server.settings import settings

me_router = APIRouter()


@me_router.post("/v1/me/set-default-namespace")
async def me_set_default_namespace(
    request: Request,
    payload: dict[str, object],
) -> dict[str, object]:
    """Persist the caller's currently-selected workspace.

    The MCP transport reads `loom.user_state` to resolve the caller's
    default workspace, so the dashboard's selection becomes the default for MCP
    clients too — single source of truth across surfaces.
    """
    import time as _time

    from server.core.ch import get_client

    subject, org = me_callers.caller_identity(request)

    namespace = str(payload.get("namespace") or "").strip()
    if not namespace:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "namespace required"},
        )

    now = naive_utc_now_seconds()
    # The row carries every column anything reads. `extraction_paused` is left to its
    # default: the column stays in the table (migrations only add), and nothing reads it.
    await get_client().insert(
        "loom.user_state",
        [
            {
                "org": org,
                "subject": subject,
                "default_namespace": namespace,
                "updated_at": now,
                "version": _time.time_ns(),
            }
        ],
    )
    return {"subject": subject, "org": org, "default_namespace": namespace}


_SHARING_WRITE_OPS = [
    "memory.set",
    "memory.bulk_set",
    "memory.update_by_id",
    "memory.pin",
    "memory.promote",
    # The ways agents actually populate a namespace. The hook plugins write almost only
    # through observe.record; a seeded namespace arrives through set_from_messages or
    # memory.import; typed records and knowledge are writes too. Without them every
    # producer would show "0 writes".
    "memory.import",
    "memory.set_from_messages",
    "memory.record.write",
    "knowledge.add",
    "observe.record",
]
# A recall is any read that pulled stored knowledge into an agent's context:
# memory.search on a memory namespace, or a code read on a repository index
# (repositories are read-only, so reads are the only activity they can ever show; counting
# memory.search alone would show every repository as "0 recalls"). Status pokes are not
# recalls.
_SHARING_RECALL_OPS = [
    "memory.search",
    "code.search",
    "code.grep",
    "code.symbol",
    "code.ask",
    "code.context_pack",
    "code.impact",
    "code.blame_range",
    "code.callers",
    "code.callees",
    "code.trace",
    "code.history",
    "code.review_brief",
    "code.bisect_candidates",
    "code.setting_facts",
    "code.verify_citations",
    "code.examples",
    "code.conventions",
]


def _fold_sharing_agent_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One row per agent × namespace, and the same summed per agent.

    A cross-namespace recall is a read whose recorded `namespace_scope` was `all_mine` — it
    spanned the caller's own namespaces (the all_mine fan-out), not only the one it was
    addressed to. Rows logged without the argument are not claimed as cross-namespace; code
    reads never are, a repository is one namespace.
    """
    per_ns: list[dict[str, Any]] = []
    by_agent: dict[str, dict[str, int]] = {}
    for r in rows:
        agent = str(r["agent"])
        writes, recalls, cross = int(r["writes"]), int(r["recalls"]), int(r["cross_ns"])
        per_ns.append(
            {
                "agent": agent,
                "namespace": str(r["namespace"]),
                "writes": writes,
                "recalls": recalls,
                "cross_namespace_recalls": cross,
            }
        )
        acc = by_agent.setdefault(agent, {"writes": 0, "recalls": 0, "cross": 0})
        acc["writes"] += writes
        acc["recalls"] += recalls
        acc["cross"] += cross
    agents = [
        {
            "agent": a,
            "writes": v["writes"],
            "recalls": v["recalls"],
            "cross_namespace_recalls": v["cross"],
        }
        for a, v in sorted(by_agent.items(), key=lambda kv: -(kv[1]["writes"] + kv[1]["recalls"]))
    ]
    return agents, per_ns


@me_router.get("/v1/me/sharing-summary")
async def me_sharing_summary(request: Request) -> dict[str, object]:
    """Aggregate multi-namespace memory sharing into one dashboard payload.

    Three datasets, all scoped to namespaces the caller has an active
    grant on (loom.namespace_grants):
      1. Per-agent activity (writes / recalls) in the last 7 days, per
         namespace and summed.
      2. Per-namespace membership + member counts.
      3. Per-namespace activity (writes / recalls) over the same window.
    """
    from server.core.ch import get_client

    # Auth-off keeps a self-minted bearer's subject so per-subject role lookups
    # stay accurate on multi-member namespaces.
    subject, org = me_callers.caller_identity(request, prefer_bearer_in_dev=True)
    subs = await me_callers.lowered_aliases(org, subject)

    ch = get_client()
    # auth_off only when we have no identifiable caller. With a verified
    # subject (even in single-user mode) the subject-filtered path is used;
    # otherwise fall back to the any(role) aggregation so the dashboard
    # surfaces something rather than an empty state.
    auth_off = not settings.auth_actually_required() and subject == "you"
    if auth_off:
        grant_subq = """
            SELECT namespace FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND subject != 'test-suite'
              AND {live}
        """.replace("{live}", LIVE_GRANT)
    else:
        grant_subq = """
            SELECT namespace FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND lower(subject) IN {subs:Array(String)}
              AND {live}
        """.replace("{live}", LIVE_GRANT)

    # 1. Per-agent activity (writes + recalls) within my namespaces, per
    #    namespace (the Flow page filters to the namespace it shows) and summed per agent.
    write_ops = _SHARING_WRITE_OPS
    recall_ops = _SHARING_RECALL_OPS
    agent_rows = await ch.query_all(
        f"""
        SELECT agent, namespace,
               countIf(operation IN {{wops:Array(String)}}) AS writes,
               countIf(operation IN {{rops:Array(String)}}) AS recalls,
               countIf(operation IN {{rops:Array(String)}}
                       AND args['namespace_scope'] = 'all_mine') AS cross_ns
        FROM loom.agent_events
        WHERE org = {{org:String}}
          AND namespace IN ({grant_subq})
          AND ts > now() - INTERVAL 7 DAY
          AND (operation IN {{wops:Array(String)}} OR operation IN {{rops:Array(String)}})
          AND success = true
          AND agent != ''
        GROUP BY agent, namespace
        ORDER BY writes + recalls DESC
        """,
        params={"org": org, "sub": subject, "subs": subs, "wops": write_ops, "rops": recall_ops},
    )
    agents_payload, agent_namespaces_payload = _fold_sharing_agent_rows(agent_rows)

    # 2. Namespace membership + member counts. Combined in Python rather
    #    than a cross-FINAL SQL JOIN; the dataset is tens of rows per user.
    my_grants_sql = (
        """
        SELECT namespace, any(role) AS role, max(granted_at) AS granted_at
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String}
          AND subject != 'test-suite'
          AND {live}
        GROUP BY namespace
        ORDER BY granted_at DESC
        """.replace("{live}", LIVE_GRANT)
        if auth_off
        else """
        SELECT namespace, role, granted_at
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String}
          AND lower(subject) IN {subs:Array(String)}
          AND {live}
        ORDER BY granted_at DESC
        """.replace("{live}", LIVE_GRANT)
    )
    my_grants = await ch.query_all(
        my_grants_sql,
        params={"org": org, "sub": subject, "subs": subs},
    )
    my_ns_set = [r["namespace"] for r in my_grants]

    member_counts: dict[str, int] = {}
    visibilities: dict[str, str] = {}
    if my_ns_set:
        member_rows = await ch.query_all(
            """
            SELECT namespace, uniqExact(subject) AS members
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND namespace IN {ns:Array(String)}
              AND {live}
            GROUP BY namespace
            """.replace("{live}", LIVE_GRANT),
            params={"org": org, "ns": my_ns_set},
        )
        member_counts = {r["namespace"]: int(r["members"]) for r in member_rows}

        vis_rows = await ch.query_all(
            """
            SELECT namespace, toString(visibility) AS visibility
            FROM loom.namespaces FINAL
            WHERE org = {org:String}
              AND namespace IN {ns:Array(String)}
            """,
            params={"org": org, "ns": my_ns_set},
        )
        visibilities = {r["namespace"]: r["visibility"] for r in vis_rows}

        # 3. Per-namespace activity (writes + recalls last 7d).
        ns_activity_rows = await ch.query_all(
            """
            SELECT
                namespace,
                sum(if(operation IN {wops:Array(String)}, 1, 0)) AS writes,
                sum(if(operation IN {rops:Array(String)}, 1, 0)) AS recalls
            FROM loom.agent_events
            WHERE org = {org:String}
              AND namespace IN {ns:Array(String)}
              AND ts > now() - INTERVAL 7 DAY
              AND success = true
            GROUP BY namespace
            """,
            params={"org": org, "ns": my_ns_set, "wops": write_ops, "rops": recall_ops},
        )
        ns_activity = {r["namespace"]: r for r in ns_activity_rows}
    else:
        ns_activity = {}

    namespaces_payload = [
        {
            "namespace": r["namespace"],
            "role": r["role"],
            "visibility": visibilities.get(r["namespace"], "private"),
            "member_count": member_counts.get(r["namespace"], 1),
            "writes_7d": int((ns_activity.get(r["namespace"]) or {}).get("writes") or 0),
            "recalls_7d": int((ns_activity.get(r["namespace"]) or {}).get("recalls") or 0),
        }
        for r in my_grants
    ]
    # Auth-off reads the whole org's grants, pulling in dormant + test
    # namespaces. Surface the top 10 by recent activity (>=5 events/7d).
    if auth_off:
        namespaces_payload = [
            n for n in namespaces_payload if (n["writes_7d"] + n["recalls_7d"]) >= 5
        ]
        namespaces_payload.sort(
            key=lambda n: -(n["writes_7d"] + n["recalls_7d"]),
        )
        namespaces_payload = namespaces_payload[:10]

    # Exclude sentinel agents (`*` SPA wildcard, `-` no-agent default) from
    # the cross-ns total so it reflects MCP/SDK activity, not dashboard browsing.
    real_agents = [a for a in agents_payload if a["agent"] not in ("*", "-")]
    total_writes = sum(p["writes_7d"] for p in namespaces_payload)
    total_recalls = sum(p["recalls_7d"] for p in namespaces_payload)
    total_cross = sum(p["cross_namespace_recalls"] for p in real_agents)

    return {
        "subject": subject,
        "org": org,
        "window_days": 7,
        "totals": {
            "writes": total_writes,
            "recalls": total_recalls,
            "cross_namespace_recalls": total_cross,
            "namespaces": len(namespaces_payload),
            "agents": len(agents_payload),
        },
        "agents": agents_payload,
        "agent_namespaces": agent_namespaces_payload,
        "namespaces": namespaces_payload,
    }


def attach(app: Any) -> None:
    """Mount the caller API on the FastAPI app."""
    app.include_router(me_router)
    app.include_router(me_service_accounts.router)


__all__ = ["attach", "me_router"]
