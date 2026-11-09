"""The caller's own tokens: `/v1/me/service_accounts{,.create,.revoke}`.

A signed-in person mints, lists and revokes tokens for their tools, agents,
scripts and CI jobs, never wider than their own grants, with no operator
round-trip.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from server.auth.delegation import Delegated, ceiling_role, delegate
from server.auth.jwt import AuthInfo
from server.auth.public_url import public_base_url
from server.auth.self_signup import AGENT_NAME_PREFIX
from server.auth.service_accounts import hand_down_lineage, revoked_row
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import BackendUnavailableError
from server.core.grant_sql import LIVE_GRANT
from server.identity.app import me_callers, namespace_classification, token_grants
from server.identity.app.caller_home import caller_org, default_namespace
from server.identity.app.restricted_access import note_restricted_grants
from server.identity.app.token_clients import client_configs
from server.operations.base import GRANT_ROLES, Role, grant_rank
from server.settings import settings

router = APIRouter()


def json_int(value: object) -> int:
    """A JSON body value that is an integer (`7`, or `7.0`); TypeError for
    anything else: a bool, a string, a fraction, an infinity, a list or null."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not an integer: {type(value).__name__}")
    if isinstance(value, float) and not value.is_integer():  # also inf and nan
        raise TypeError(f"not an integer: {value!r}")
    return int(value)


def parse_ttl_days(ttl_raw: object) -> int | None:
    """`ttl_days` of a minted token in seconds: None = the delegation default
    (90 days); 0 = never expires; else 1..3650 days; anything else is a 422."""
    if ttl_raw is None:
        return None
    try:
        ttl_days = json_int(ttl_raw)
        if ttl_days < 0 or ttl_days > 3650:
            raise ValueError
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "ttl_days must be 1..3650, 0 for never, or null for 90",
            },
        ) from e
    return ttl_days * 86400


def _requested_roles(payload: dict[str, object]) -> dict[str, str | None]:
    """The body's namespaces in read-priority order, each with the role asked
    for there (None: the caller's live grant); 422 on a malformed one. A
    missing list, or `"*"`, asks for every namespace the caller belongs to."""
    raw_namespaces = payload.get("namespaces")
    if raw_namespaces is None or raw_namespaces == "*":
        raw_namespaces = ["*"]
    if not isinstance(raw_namespaces, list) or not raw_namespaces:
        raise _invalid('namespaces must be "*" or a non-empty array')

    default_role_raw = payload.get("role")
    if default_role_raw is not None and str(default_role_raw) not in GRANT_ROLES:
        raise _invalid("role must be viewer | writer | admin")
    default_role = str(default_role_raw) if default_role_raw is not None else None

    # Parse ordered entries (order = read priority). Each is a bare string or
    # {namespace, role?}. Duplicate namespaces collapse to first occurrence so
    # priority stays stable; the last-seen role wins for that namespace.
    requested_role: dict[str, str | None] = {}
    for entry in raw_namespaces:
        if isinstance(entry, dict):
            ns = str(entry.get("namespace") or "").strip()
            r_raw = entry.get("role")
            r = str(r_raw) if r_raw is not None else default_role
        else:
            ns = str(entry).strip()
            r = default_role
        if not ns:
            continue
        if r is not None and r not in GRANT_ROLES:
            raise _invalid(f"role for {ns} must be viewer | writer | admin")
        requested_role[ns] = r
    if not requested_role:
        raise _invalid('namespaces must be "*" or a non-empty array')
    if "*" in requested_role and any(r is not None for r in requested_role.values()):
        # `*` reads through the caller's live grants, at the caller's role in each.
        raise _invalid('a role needs a list without "*": a "*" token has your role everywhere')
    return requested_role


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": message})


async def _bound_trackers(org: str, subject: str, parent: AuthInfo, repos: list[str]) -> list[str]:
    """The restricted tracker records bound to `repos` that the caller holds a grant on and
    the parent token reaches; the history code ops read issues and PRs there."""
    from server.identity.app import namespace_kinds
    from server.tracker.app.binding import (
        operator_repositories,
        refresh_overrides,
        tracker_namespaces_for,
    )

    out: list[str] = []
    if not repos:
        return out
    # Their bound trackers come from the operator repositories, unknown until that read.
    kinds = await namespace_kinds.kind_map(org)
    token_grants.refuse_unread_code(org, repos, kinds, configured_too=True)
    await refresh_overrides()  # the links as of the refresh interval, branches included
    operator = operator_repositories()  # once for every name
    for ns in repos:
        for tracker in tracker_namespaces_for(org, ns, operator=operator):
            if tracker in out or tracker in repos or ceiling_role(parent, org, tracker) is None:
                continue
            if not await namespace_classification.is_restricted(org, tracker):
                continue
            if await me_callers.caller_role_on(org, tracker, subject):
                out.append(tracker)
    return out


async def _name_holder(org: str, name: str) -> str | None:
    """Who made the token, not yet revoked or expired, that holds `name` in
    `org`; None when it is free. A new row would replace its row, leaving it
    live and listed nowhere. Read under the name's claim, as of every write."""
    from server.core.ch import get_client

    row = await get_client().query_one(
        "SELECT created_by FROM loom.service_accounts FINAL WHERE org = {org:String} "
        "AND name = {name:String} AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
        params={"org": org, "name": name},
    )
    return str(row["created_by"]) if row is not None else None


def token_name(raw: object, default: str = "") -> str:
    """A new token's name in its owner's token list, `default` when `raw` is blank; 422
    for one off its shape."""
    name = str(raw or "").strip() or default
    if not 1 <= len(name) <= 64:
        raise _invalid("name must be 1-64 chars")
    if "\x00" in name:  # reserved: revocation cutoffs key on a NUL (mint_token refuses it)
        raise _invalid("name cannot hold a NUL character")
    if ":" in name:  # reserved: `sa:<trust>:<agent>` is a machine trust's agent (auth.agents)
        raise _invalid("name cannot hold a ':'")
    if name.startswith(AGENT_NAME_PREFIX):  # self-signup's, written without this check
        raise _invalid(f"names starting {AGENT_NAME_PREFIX!r} are agents'")
    return name


async def refuse_taken_name(org: str, name: str, subject: str) -> None:
    """409 when a live token holds `name` in `org`. Called under the name's claim."""
    holder = await _name_holder(org, name)
    if holder is not None:
        # Names are one per org; whose token holds one is not the caller's to learn.
        yours = await me_callers.made_by_caller(org, subject, holder)
        taken = f"your token {name!r} is live" if yours else "that name is in use"
        raise HTTPException(
            status_code=409,
            detail={"code": "TOKEN_NAME_TAKEN", "message": f"{taken}; choose another name"},
        )


def generated_name(now: datetime) -> str:
    """The name of a token made without one: its day and 48 random bits, so
    names made in bulk do not meet in practice (a clash would be a 409)."""
    return f"token-{now:%Y-%m-%d}-{secrets.token_hex(6)}"


def new_token_subject(org: str, name: str, creator: str) -> str:
    """A new token's own `sa:` subject, so revoking it ends this token alone."""
    sa_hash = hashlib.sha1(f"{org}:{name}:{creator}:{time.time_ns()}".encode()).hexdigest()[:6]
    return f"sa:{name}-{sa_hash}"


async def save_token(
    name: str,
    sa_subject: str,
    delegated: Delegated,
    *,
    created_by: str,
    now: datetime,
) -> None:
    """List the token in its owner's token list (`loom.service_accounts`).
    Called under the name's claim."""
    from server.core.ch import get_client
    from server.core.versions import next_version

    ns_roles = delegated.ns_roles or {}
    # `roles` keeps the distinct effective roles for the list view; `ns_roles`
    # the authoritative per-namespace map (JSON), when the token has one.
    roles = sorted(set(ns_roles.values()), key=grant_rank) if ns_roles else delegated.roles
    ch = get_client()
    await hand_down_lineage(delegated.org, name)
    # Past every row the name has held, whichever host's clock wrote it: a
    # revoked row from a host running ahead would otherwise replace this one.
    version = await next_version(ch, "loom.service_accounts", {"org": delegated.org, "name": name})
    lineage = {created_by, delegated.creator, *delegated.parent_subjects} - {""}
    await ch.insert(
        "loom.service_accounts",
        [
            {
                "org": delegated.org,
                "name": name,
                "sa_subject": sa_subject,
                "namespaces": delegated.namespaces,
                "roles": roles,
                "ns_roles": json.dumps(ns_roles),
                "ttl_days": delegated.ttl_days,
                "created_by": created_by,
                "created_at": now,
                "expires_at": delegated.expires_at(now),
                "revoked_at": None,
                "revoked_by": "",
                "version": version,
                "kind": "service_account",
                "client": "",
                # The gate and the namespace listings read this flag: a token
                # reads through its creator's live grants (server/auth/service_accounts.py).
                "inherit_creator_grants": True,
                "lineage": sorted(lineage),
            }
        ],
    )


def dev_bearer(request: Request) -> AuthInfo | None:
    """With auth off, the principal of a valid bearer the request sends; None
    for no bearer or one that does not verify, which auth off ignores. 503 while
    a bearer cannot be checked yet, as `verify_bearer` answers."""
    from server.auth.bearer import auth_unavailable, bearer_token, gateway_refused
    from server.auth.gateway import is_end_user
    from server.auth.jwt import AuthUnavailable, TokenError, verify_token
    from server.observability.metrics import record_auth_refusal

    try:
        token = bearer_token(request.headers.get("Authorization"))
        info = None if token is None else verify_token(token)
    except AuthUnavailable as e:
        record_auth_refusal(e, transport="http")
        raise auth_unavailable(e) from e
    except TokenError:
        return None
    if info is not None and is_end_user(info):
        raise gateway_refused()
    return info


def dev_caller(request: Request, params: Mapping[str, object]) -> tuple[str, str]:
    """(subject, org) of the caller in dev mode (auth off): a valid bearer's,
    else the request's `subject` and `org`, else "dev" in "default". Every
    route that saves, lists or revokes a token reads it, so a token minted
    with auth off is in the list it is revoked from."""
    info = dev_bearer(request)
    if info is not None:
        return info.subject, info.org
    return str(params.get("subject") or "dev"), str(params.get("org") or "default")


def dev_parent(request: Request, payload: dict[str, object]) -> AuthInfo:
    """The parent a token is delegated from in dev mode (auth off): a flat
    writer for `dev_caller`, issued now. A bearer only names the owner; its
    scope and lifetime do not cap the token, as with token.mint."""
    subject, org = dev_caller(request, payload)
    return AuthInfo(
        subject=subject,
        org=org,
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=False,
        issued_at=int(time.time()),
    )


@router.post("/v1/me/service_accounts.create")
async def me_service_accounts_create(
    request: Request,
    payload: dict[str, object],
) -> dict[str, object]:
    """Mint a token for the caller's own tools, agents, scripts and CI jobs,
    never wider than the caller's grants, and hand it over with the setup of
    every supported MCP client.

    Body:
      name?: str          # its name in the token list; blank: token-<day>-<hex>
      namespaces?: "*" | [ ... ]
                          # "*" (the default): every namespace the caller
                          #   belongs to, now and later, at the caller's role
                          #   in each. A name before "*" is where it saves;
                          #   restricted namespaces it names are read too.
                          # A list without "*": exactly these, ORDERED (order =
                          #   read priority), each a bare "namespace" or
                          #   {"namespace": str, "role"?: str}. Role per entry
                          #   defaults to the top-level `role`, else the
                          #   caller's live grant on that namespace.
      role: str|null      # default role for bare entries (viewer|writer|admin)
      ttl_days: int|null  # default and null: 90 days; 0: never expires

    Each namespace's role is delegated from the caller's token
    (`server.auth.delegation.delegate`): at most the caller's live grant on
    that namespace, and the caller's token role there when that token is
    itself delegated or per-namespace, or the namespace is restricted. A
    list token carries an `ns_roles` claim (the authoritative per-namespace
    role); a `*` token one role, rechecked against the caller's live grant on
    every call. A restricted namespace it names gets a direct grant of the
    token's own, capped at the caller's own grant there and revoked with it;
    a restricted repository brings its bound restricted tracker record.
    Returns the JWT exactly once, with where calls that name no namespace
    save (`caller_home.default_namespace`).
    """
    caller: AuthInfo | None = None  # auth off: a mint writes no audit row
    if settings.auth_actually_required():
        parent = caller = me_callers.require_authed_info(request)
    else:
        parent = dev_parent(request, payload)
    subject, org = parent.subject, parent.org

    name = token_name(payload.get("name"), default=generated_name(naive_utc_now_seconds()))
    requested_role = _requested_roles(payload)
    ttl_seconds = parse_ttl_days(payload.get("ttl_days"))
    everything = "*" in requested_role
    # A restricted repository's bound tracker record is named beside it, read-only.
    named = [
        ns
        for ns in requested_role
        if ns != "*" and await namespace_classification.is_restricted(org, ns)
    ]
    for tracker in await _bound_trackers(org, subject, parent, named):
        requested_role.setdefault(tracker, None if everything else "viewer")

    from server.auth.grant_claims import service_account_change

    async with service_account_change(org, name):
        await refuse_taken_name(org, name, subject)
        delegated = await delegate(
            parent,
            org=org,
            requested=requested_role,
            scoped=not everything,
            ttl_seconds=ttl_seconds,
            is_service_account=True,
        )
        # Read before anything is saved, so a refusal or a failed read strands no token.
        restricted_roles = await token_grants.restricted_roles(
            org, subject, delegated, requested_role
        )
        is_code = await token_grants.code_namespace_predicate(
            org, list(restricted_roles), namespace_classification.is_restricted
        )
        sa_subject = new_token_subject(org, name, subject)
        minted = delegated.mint(sa_subject)
        now = naive_utc_now_seconds()  # after the mint: the row is never older than the token's iat
        expires_at = delegated.expires_at(now)
        await save_token(name, sa_subject, delegated, created_by=subject, now=now)
    # A restricted namespace named here is read only when the token's own subject holds a
    # direct grant: write it now, capped, revoked with the token; a name in the claim alone
    # would have every read of it refused.
    restricted_granted = await token_grants.grant_restricted_to_token(
        org, sa_subject, subject, restricted_roles, is_code_namespace=is_code
    )
    await note_restricted_grants(
        caller,
        op="me.service_accounts.create",
        org=org,
        namespaces=restricted_granted,
        granted_to=sa_subject,
    )

    token = delegated.as_auth(sa_subject)
    try:
        # The org the MCP transport resolves this token in: an org-wide parent's `*`
        # child lands in the self-signup org there.
        saves_to: str | None = await default_namespace(token, caller_org(token) or org)
    except BackendUnavailableError:
        # The token is minted and listed: hand it over without a name rather than
        # fail, or a retry would mint a second one. Each call resolves it.
        saves_to = None
    mcp_url = f"{public_base_url(request)}/mcp"
    return {
        "token": minted,
        "name": name,
        "sa_subject": sa_subject,
        "org": org,
        # The claim as minted: `*` (and what is named beside it), or the list it reads.
        # A `*` asked under a narrower sign-in becomes that sign-in's list.
        "namespaces": delegated.namespaces,
        "ns_roles": delegated.ns_roles or {},
        # The one role of a token without `ns_roles`; [] for a list with a role each.
        "roles": delegated.roles,
        "reads_everything": "*" in delegated.namespaces,
        # Where calls that name no namespace save; "": none, each call names one;
        # null: not readable now.
        "saves_to": saves_to,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "restricted_namespaces": restricted_granted,
        "mcp_url": mcp_url,
        "clients": client_configs(mcp_url, minted),
    }


@router.get("/v1/me/service_accounts")
async def me_service_accounts_list(request: Request) -> dict[str, object]:
    """List SAs the caller can see: ones they created, or ones scoped
    to a namespace they hold admin on."""
    from server.core.ch import get_client

    if settings.auth_actually_required():
        subject, org = me_callers.require_authed_caller(request)
    else:
        subject, org = dev_caller(request, request.query_params)

    # Every spelling of the caller, in any case, as grants match a subject.
    subs = await me_callers.lowered_aliases(org, subject)
    rows = await get_client().query_all(
        """
        WITH admin_ns AS (
            SELECT namespace
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND lower(subject) IN {subs:Array(String)}
              AND role = 'admin'
              AND {live}
        ),
        sa_last_used AS (
            -- One row per subject, kept by a materialized view on the ledger:
            -- a few kB instead of a month of loom.agent_events,
            -- whose sort key does not carry the subject (~10 MB for this read).
            SELECT subject AS sa_subject, maxMerge(last_seen) AS last_used
            FROM loom.subject_last_seen
            WHERE org = {org:String}
              AND subject LIKE 'sa:%'
            GROUP BY subject
        )
        SELECT
            s.name AS name, s.sa_subject AS sa_subject,
            s.namespaces AS namespaces, s.roles AS roles,
            s.ttl_days AS ttl_days, s.created_by AS created_by,
            s.created_at AS created_at, s.expires_at AS expires_at,
            s.revoked_at AS revoked_at, s.revoked_by AS revoked_by,
            s.kind AS kind, s.client AS client, s.ns_roles AS ns_roles,
            lu.last_used AS last_used_at
        -- Filter + FINAL in a subquery, THEN join: CH 26.5's analyzer can't
        -- resolve a WHERE on a FINAL table that is also a JOIN side.
        FROM (
            SELECT name, sa_subject, namespaces, roles, ttl_days, created_by,
                   created_at, expires_at, revoked_at, revoked_by, kind, client,
                   ns_roles
            FROM loom.service_accounts FINAL
            WHERE org = {org:String}
              AND (
                  lower(created_by) IN {subs:Array(String)}
                  OR arrayExists(n -> n IN (SELECT namespace FROM admin_ns), namespaces)
              )
            ORDER BY created_at DESC
            LIMIT 200
        ) AS s
        LEFT JOIN sa_last_used AS lu ON s.sa_subject = lu.sa_subject
        ORDER BY s.created_at DESC
        LIMIT 200
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "subs": subs},
    )

    def _iso(v: datetime | None) -> str | None:
        # A token never used joins no row: its last use reads as the epoch.
        return v.isoformat() if v and v.year > 1970 else None

    return {
        "rows": [
            {
                "name": r["name"],
                "sa_subject": r["sa_subject"],
                "namespaces": r["namespaces"],
                "roles": r["roles"],
                "ttl_days": r["ttl_days"],
                "created_by": r["created_by"],
                "created_at": _iso(r.get("created_at")),
                "expires_at": _iso(r.get("expires_at")),
                "revoked_at": _iso(r.get("revoked_at")),
                "revoked_by": r.get("revoked_by") or "",
                "last_used_at": _iso(r.get("last_used_at")),
                # Served for build N-1's dashboard, which labels a row by them.
                "kind": r.get("kind") or "service_account",
                "client": r.get("client") or "",
                "ns_roles": me_callers.parse_ns_roles(r.get("ns_roles")),
                # Made by the caller under any spelling of theirs, not by another person
                # whose token names a namespace the caller administers.
                "yours": str(r["created_by"]).lower() in subs,
            }
            for r in rows
        ],
    }


@router.post("/v1/me/service_accounts.revoke")
async def me_service_accounts_revoke(
    request: Request,
    payload: dict[str, object],
) -> dict[str, object]:
    """Mark an SA as revoked and revoke its subject, so its token stops
    verifying. Caller must be the creator or admin on at least one of the
    SA's namespaces."""
    from server.core.ch import get_client

    if settings.auth_actually_required():
        subject, org = me_callers.require_authed_caller(request)
    else:
        subject, org = dev_caller(request, payload)

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "name required"},
        )

    from server.auth.grant_claims import service_account_change

    # Held as create holds it: a create of this name between the read and the
    # write would be replaced by the revoked copy of the row read here.
    async with service_account_change(org, name):
        ch = get_client()
        row = await ch.query_one(
            """
            SELECT sa_subject, namespaces, created_by, created_at,
                   expires_at, ttl_days, roles, kind, client,
                   inherit_creator_grants, ns_roles, revoked_at, version, lineage
            FROM loom.service_accounts FINAL
            WHERE org = {org:String} AND name = {name:String}
            LIMIT 1
            """,
            params={"org": org, "name": name},
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NOT_FOUND",
                    "message": f"service account {name!r} not found",
                },
            )

        # Its maker under any spelling of theirs, as the list shows it.
        authorized = await me_callers.made_by_caller(org, subject, str(row["created_by"]))
        if not authorized:
            for ns in row["namespaces"]:
                if await me_callers.caller_admin_of(org, ns, subject):
                    authorized = True
                    break
        if not authorized:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "AUTH_INSUFFICIENT",
                    "message": "must be the creator or admin on a scoped namespace",
                },
            )

        # Revoked already (a retry, a second click): its revocation stands as it was.
        now = row["revoked_at"] or naive_utc_now_seconds()
        if not row["revoked_at"]:
            await ch.insert(
                "loom.service_accounts",
                [
                    revoked_row(
                        {**row, "org": org, "name": name}, revoked_at=now, revoked_by=subject
                    )
                ],
            )

    # The row keeps no jti, so revoke by `sub` (the sa_subject); its children
    # die with it through `parent_subjects`. Through `revoke`, so this replica
    # holds it at once, whatever a refresh in flight reads.
    from server.auth.revocation import revoke

    await revoke(
        str(row["sa_subject"]),
        expires_at=row.get("expires_at"),
        reason=f"service_account.revoke name={name}",
        revoked_by=subject,
    )
    # The token's own restricted grants die with it.
    grants_revoked = await token_grants.revoke_token_grants(org, str(row["sa_subject"]))

    return {
        "revoked": True,
        "name": name,
        "revoked_at": now.isoformat(),
        "grants_revoked": grants_revoked,
    }
