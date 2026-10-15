"""Self-serve dashboard: the /me surface.

The static-SPA routes (`GET /me`, `/me/`, `/me/{spa_path:path}`) plus the
`/me/assets` StaticFiles mount that serves the built Vite bundle, and the
`/v1/me/*` JSON API the SPA calls: per-user state and the sharing summary
here, MCP-client setup in `me_setup.py`, service-account self-mint in
`me_service_accounts.py`. `attach` mounts all of it.

These endpoints authenticate through `me_callers`, not the operation
registry's role gate.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from server.core.datetimes import naive_utc_now_seconds
from server.core.grant_sql import LIVE_GRANT
from server.core.paths import FRONTEND_DIST
from server.identity.app import me_callers, me_service_accounts, me_setup
from server.settings import settings

me_router = APIRouter()


_FRONTEND_DIST = FRONTEND_DIST


def dashboard_csp(nonce: str) -> str:
    """The /me page's Content-Security-Policy. Scripts run only from the
    bundle's own origin or the one boot snippet carrying `nonce`; a document
    preview is a blob: frame or image, never a script source."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        # Vite inlines small font subsets as data: URLs.
        "font-src 'self' data:; "
        "img-src 'self' data: blob:; "
        "frame-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )


def _js_string(value: str) -> str:
    """Render `value` as a JS string literal safe to inline in a `<script>`.

    Quote-only escaping isn't enough for operator-supplied config: a value
    ending in a backslash escapes its own closing quote, and the resulting
    SyntaxError takes out *every* boot global (the SPA then falls back to the
    login gate on a deployment that has no login). `json.dumps` covers quotes,
    backslashes and control characters; the `</` rewrite covers `</script>`,
    which ends the element at the HTML level no matter how the JS is escaped;
    the `<!--` rewrite keeps the HTML tokenizer out of script-data-escaped
    state, where the snippet's real `</script>` would stop closing the element.
    """
    return json.dumps(value).replace("</", "<\\/").replace("<!--", "<\\!--")


@me_router.get("/me/favicon.svg")
async def favicon() -> Response:
    """Tab icon. Needs its own route: it lives at the dist root (not under the
    /me/assets mount), so without it the /me/{spa_path:path} catch-all below
    returns index.html and the browser gets HTML instead of an SVG — no icon."""
    from fastapi.responses import FileResponse

    path = _FRONTEND_DIST / "favicon.svg"
    if not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/svg+xml")


@me_router.get("/me")
@me_router.get("/me/")
@me_router.get("/me/{spa_path:path}")
async def memory_dashboard(request: Request, spa_path: str = "") -> Response:
    # spa_path exists only so the server returns the SPA for every /me/*
    # URL instead of 404'ing on direct loads / refreshes; the React app
    # reads window.location.pathname itself.
    from fastapi.responses import RedirectResponse

    # A bare /me serves the SPA like every other path: the client's landing rule chooses the
    # first view from the token's namespaces — Setup for a first visit, Repositories for a
    # token that names only repositories. A server redirect to /me/setup would make every
    # visit an explicit Setup deep link and bypass that rule.
    # Federated-deployment escape hatch: redirect /me to an aggregator's
    # entry point. Auth-disabled mode skips this — the solo user IS the
    # intended /me audience there.
    if settings.dashboard_redirect_url and settings.auth_actually_required():
        return RedirectResponse(url=settings.dashboard_redirect_url, status_code=302)
    from fastapi.responses import HTMLResponse, PlainTextResponse

    from server.auth.bearer import bearer_token
    from server.auth.jwt import TokenError

    try:
        has_bearer = bearer_token(request.headers.get("Authorization")) is not None
    except TokenError:
        has_bearer = False

    # auth_actually_required, not auth_required: LOOM_IDENTITY_PROVIDER=
    # single-user with auth_required left true (common in prod-derived dev
    # configs) is still an auth-off deployment — the transport accepts
    # tokenless calls, so the SPA must get __LOOM_AUTH_DISABLED=true and the
    # namespace row must exist.
    auth_disabled = not settings.auth_actually_required()

    # Ensure the auth-disabled default namespace (`dev/local` unless
    # LOOM_DEFAULT_NAMESPACE overrides it) has a backing row in
    # loom.namespaces. Without it, namespace.list_mine returns nothing and a
    # future auth-enabled migration sees orphaned data with no owner. Cached
    # process-wide (also runs from the lifespan), so this is a bool check on
    # every page load after the first.
    default_ns = settings.default_namespace
    if auth_disabled:
        from server.auth.default_namespace import ensure_default_namespace_cached

        await ensure_default_namespace_cached()
    # Self-signup availability rides the boot snippet rather than being probed: probing by
    # POSTing a real signup would be a read that writes (claiming a namespace on a fresh
    # instance, then a 409 on every later load). The server already knows the answer.
    # With SSO on, the sign-in screen is one button to the identity provider:
    # password sign-in and self-signup are both refused server-side.
    sso = settings.oauth_login_provider == "oidc"
    signup_open = settings.self_signup == "open" and not sso
    nonce = secrets.token_urlsafe(16)
    boot_snippet = (
        f'<script nonce="{nonce}">'
        f"window.__LOOM_PROXY_AUTH = {'true' if has_bearer else 'false'};"
        f"window.__LOOM_AUTH_DISABLED = {'true' if auth_disabled else 'false'};"
        f"window.__LOOM_SELF_SIGNUP = {'true' if signup_open else 'false'};"
        f"window.__LOOM_SSO = {'true' if sso else 'false'};"
        f"window.__LOOM_DEFAULT_NAMESPACE = {_js_string(default_ns)};"
        f"</script>"
    )

    built_index = _FRONTEND_DIST / "index.html"
    if not built_index.is_file():
        return PlainTextResponse(
            "Dashboard not built. Run `npm --prefix frontend run build` "
            "or use the published Docker image.",
            status_code=503,
        )
    html = built_index.read_text()
    if "</head>" in html:
        html = html.replace("</head>", boot_snippet + "\n</head>", 1)
    else:
        html = boot_snippet + html
    # no-store: the HTML references content-hashed assets that rotate per
    # build; a cached HTML would point at a bundle hash that no longer exists.
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": dashboard_csp(nonce)},
    )


@me_router.post("/v1/me/set-default-namespace")
async def me_set_default_namespace(
    request: Request,
    payload: dict[str, object],
) -> dict[str, object]:
    """Persist the caller's currently-selected workspace.

    The MCP transport reads `loom.user_state` to resolve the caller's
    default workspace, so /me's selection becomes the default for MCP
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
    # through observe.record; a seeded namespace arrives through set_from_messages; typed
    # records and knowledge are writes too. Without them every producer would show "0 writes".
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
    subs = await me_callers.subject_aliases_of(org, subject)

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
              AND subject IN {subs:Array(String)}
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
          AND subject IN {subs:Array(String)}
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
    """Mount the /me surface on the FastAPI app.

    Mount order is load-bearing: `/me/assets` (hashed JS/CSS) must mount
    BEFORE me_router's `/me/{spa_path:path}` catch-all, or asset requests
    match the catch-all and get HTML back (MIME mismatch). The .is_dir()
    guard lets `npm run dev` work without a built bundle on disk.
    """
    if _FRONTEND_DIST.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/me/assets",
            StaticFiles(directory=str(_FRONTEND_DIST / "assets")),
            name="me_assets",
        )

    app.include_router(me_router)
    app.include_router(me_setup.router)
    app.include_router(me_service_accounts.router)


__all__ = ["attach", "me_router"]
