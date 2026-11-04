"""What an operator does to accounts and bots, shared by the host CLI
(`loom sso`, `loom bot`) and the platform-admin API (`/v1/admin/sso.*`,
`/v1/admin/bots.*`), so the two cannot drift."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from server.auth import agents, machines, revocation, sso_links
from server.auth.grant_claims import account_change, bot_change, trusts_change
from server.core.naming import SSO_NAMESPACE_PREFIX
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)

_BOT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# A person's service accounts are `sa:<name>-<6 hex>`: a bot named so would
# share their subject, and a disable would cut off their tokens.
_SERVICE_ACCOUNT_SUFFIX = re.compile(r"-[0-9a-f]{6}")


# Claims every token of an issuer carries: alone they tell no workload apart.
_STANDARD_CLAIMS = frozenset({"iss", "aud", "exp", "iat", "nbf", "jti"})


# ── carrying password accounts over to SSO ────────────────────────────────


async def adopt_plan(domain: str, org: str = "", skip: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Every unlinked password account at `domain`, with why it would be
    skipped ('' to adopt). `skip` names emails the operator leaves out, such
    as a bot's: no person signs in as it, and adopting it would revoke the
    tokens it runs on. Raises ValueError when a skip matches no account."""
    left_out = {e.strip().lower() for e in skip}
    if "" in left_out:
        raise ValueError("a skip needs an email")
    rows = await sso_links.unlinked_in_domain(domain, org)
    by_email: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_email.setdefault(r["contact_email"], []).append(r)
    unmatched = left_out - {e.lower() for e in by_email}
    if unmatched:
        # A mistyped bot's email would leave the bot in, and adopting it revokes its tokens.
        raise ValueError(
            f"no unlinked account at {domain} has email {', '.join(sorted(unmatched))}"
        )
    on_sso = await sso_links.reached_by_sso(list(by_email))
    return [
        {
            "email": email,
            "org": r["org"],
            "namespace": r["namespace"],
            "owner": r["owner_email"],
            "created_at": str(r["created_at"])[:10],
            "skip": "left out by the operator"
            if email.lower() in left_out
            else _skip_reason(accounts, email in on_sso),
        }
        for email, accounts in by_email.items()
        for r in accounts
    ]


def _skip_reason(accounts: list[dict[str, Any]], on_sso: bool) -> str:
    """Each of these would land an SSO sign-in on someone else's subject, or on none."""
    if on_sso:
        return "already signs in with SSO; link by its password instead"
    if len({a["org"] for a in accounts}) > 1:
        return "accounts in several orgs"
    if len({a["owner_email"] for a in accounts}) > 1:
        return "accounts of several owners"
    return ""


async def adopt(plan: list[dict[str, Any]], *, by: str, keep_tokens: bool) -> int:
    """Link the plan's adoptable accounts; returns how many. Only once SSO is
    on: before, the account's password still signs in, and whoever registered
    the address would mint a token past the cutoff."""
    if settings.oauth_login_provider != "oidc":
        raise ValueError(
            "turn SSO on first (LOOM_OAUTH_LOGIN_PROVIDER=oidc), then adopt with the "
            "servers' settings: where they run, or through /v1/admin/sso.adopt"
        )
    adoptable: dict[str, list[dict[str, Any]]] = {}
    for a in plan:
        if not a["skip"]:
            adoptable.setdefault(a["email"], []).append(a)
    linked = 0
    cutoff: datetime | None = None
    for email, accounts in adoptable.items():
        org, owner = accounts[0]["org"], accounts[0]["owner"]
        # Whoever registered the address may hold tokens from before; the person
        # signs in again through the provider. Revoked before the link lands, so
        # an adopt stopped between the two is redone whole by the next one.
        if not keep_tokens:
            # Past every token issued so far on any replica, as a bot's cutoff is.
            cutoff = revocation.from_now_on()
            await revocation.cut_off(owner, issued_before=cutoff, reason="sso adopt", revoked_by=by)
        live = await sso_links.link_live(email, [(org, a["namespace"]) for a in accounts], by)
        if not live:
            continue  # released meanwhile: nothing was linked
        linked += len(live)
        await audit.emit(
            "auth.sso_linked",
            org=org,
            namespace="-",
            actor=by,
            subject={
                "email": email,
                "by": by,
                "subject": owner,
                "namespaces": [ns for _, ns in live],
                "tokens_revoked": not keep_tokens,
            },
        )
    if cutoff is not None:  # a sign-in once adopt returns, on any replica, is past every cutoff
        await revocation.wait_out(cutoff)
    return linked


async def _verified_elsewhere(org: str, namespace: str, email: str) -> bool:
    """Whether another active account in `org` holds `email` verified (an SSO
    account's, or one linked to it), so grants under it are that account's."""
    from server.core.ch import get_client, own_write_reads
    from server.core.grant_sql import VERIFIED_EMAIL_SQL

    with own_write_reads():
        row = await get_client().query_one(
            f"""
            SELECT 1 AS held FROM loom.namespaces FINAL
            WHERE org = {{org:String}} AND namespace != {{ns:String}} AND active = 1
              AND contact_email = {{email:String}} AND {VERIFIED_EMAIL_SQL}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace, "email": email},
        )
    return row is not None


async def link_plan(email: str, org: str, namespace: str) -> dict[str, Any]:
    """What `link_account` would change: the account row, the address it names
    now, the grants under it to carry to the subject revoked (its revoked ones,
    or all of them when it is another account's verified email, `elsewhere`).
    Raises ValueError when it cannot take `email`."""
    from server.core.ch import get_client, own_write_reads
    from server.identity.store import namespaces

    if settings.oauth_login_provider != "oidc":
        # SSO on closes self-signup: nobody can register the freed address and
        # inherit what it still names.
        raise ValueError(
            "turn SSO on first (LOOM_OAUTH_LOGIN_PROVIDER=oidc), then link with the "
            "servers' settings: where they run, or through /v1/admin/sso.link"
        )
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError(f"{email!r} is not an email address")
    with own_write_reads():
        row = await namespaces.fetch_row(org, namespace)
        if row is None or not row["active"] or not row["password_hash"]:
            raise ValueError(f"{org}/{namespace} is not an active password account")
        was = str(row["contact_email"])
        if was == email:
            if (org, namespace) in await sso_links.linked(email):
                raise ValueError(f"{org}/{namespace} is linked to {email} already")
            # Registered under this address, or a link stopped before linking it.
            raise ValueError(f"{org}/{namespace} already names {email}: adopt it instead")
        other = await get_client().query_one(
            "SELECT org, namespace FROM loom.namespaces FINAL "
            "WHERE contact_email = {email:String} AND active = 1 LIMIT 1",
            params={"email": email},
        )
        if other is not None:
            # Two accounts naming the address: its sign-in would reach neither.
            taken = f"--org {other['org']} --namespace {other['namespace']}"
            raise ValueError(
                f"{email} is already {other['org']}/{other['namespace']}'s (an earlier SSO "
                f"sign-in made it?): `loom claim-release {taken}` first; its memories stay "
                "behind in it, and grants made to it must be made again"
            )
        grants = await get_client().query_all(
            "SELECT * FROM loom.namespace_grants FINAL WHERE org = {org:String} "
            "AND lower(subject) = {was:String}",
            params={"org": org, "was": was.lower()},  # grants match in any case, as at the gate
        )
    pending = [g for g in grants if g["revoked_at"] is None and g["status"] == "pending"]
    live = [g for g in grants if g["revoked_at"] is None]
    was_linked = elsewhere = False
    if was and live:
        with own_write_reads():  # a link another process just made
            was_linked = (org, namespace) in await sso_links.linked(was)
        # An unlinked password account's address was never verified here: its rows
        # do not admit this account. When it is another account's verified email,
        # its rows (a pending request too) are that account's and the link leaves
        # them in place; else they admit no one.
        elsewhere = not was_linked and await _verified_elsewhere(org, namespace, was)
        if elsewhere:
            live = pending = []
    if was and pending:
        named = ", ".join(g["namespace"] for g in pending)
        raise ValueError(
            f"{was} asked to join {named}: reject the request (or grant "
            f"{row['owner_email']} there and then reject it) first"
        )
    if was and live:
        named = ", ".join(f"{g['namespace']} ({g['role']})" for g in live)
        if was_linked:
            # A linked address's grants admit the person; untied, they would reach no one.
            raise ValueError(
                f"{was} holds grants of its own: {named}; grant them to "
                f"{row['owner_email']} and revoke them first"
            )
        raise ValueError(
            f"{was} holds grants of its own: {named}; they admit no one (the "
            f"address was never verified): grant them to {row['owner_email']} if "
            "they were meant for this account, and revoke them first"
        )
    # Each row under the old address capped this account's tokens at rank 0 while
    # the unverified tie held: a revoked one, or any when the address is another
    # account's, is carried to the subject revoked, so the cap outlives the tie.
    revoked = [g for g in grants if elsewhere or g["revoked_at"] is not None] if was else []
    if revoked:
        with own_write_reads():
            own = await get_client().query_all(
                "SELECT DISTINCT namespace FROM loom.namespace_grants FINAL "
                "WHERE org = {org:String} AND lower(subject) = lower({owner:String})",
                params={"org": org, "owner": str(row["owner_email"])},
            )
        # Where the subject has a row of its own it caps its tokens already.
        mine = {r["namespace"] for r in own}
        revoked = [g for g in revoked if g["namespace"] not in mine]
    return {"row": row, "was": was, "revoked": revoked, "elsewhere": elsewhere}


async def link_account(
    email: str, org: str, namespace: str, *, by: str
) -> tuple[str, list[str], bool]:
    """Let SSO sign-ins with `email` reach the password account (org, namespace)
    registered under another address (a personal one, a `+tag`): its contact
    becomes `email` and it is linked, so the person keeps its subject, grants,
    namespaces and tokens. The operator vouches that `email` is theirs. A grant
    revoked under the old address (any, when it is another account's verified
    email) stops capping the person's tokens once the address is untied, so each
    is carried to the subject, revoked. Holds the address as JIT holds it, so no
    SSO sign-in makes a second account for it meanwhile. Returns the address it
    had, the namespaces carried, and whether that address is another account's
    verified email, as read under the account and email claims."""
    from server.auth.self_signup import CLAIM_TTL_S, email_claim
    from server.core import leases
    from server.core.ch import get_client
    from server.core.errors import GrantBusyError

    email = email.strip().lower()
    async with (
        account_change(org, namespace),
        leases.holding(
            email_claim(settings.oidc_jit_org, email), ttl_s=CLAIM_TTL_S, wait_s=10.0
        ) as held,
    ):
        if not held:
            raise GrantBusyError(f"a sign-in for {email} is running; retry shortly")
        plan = await link_plan(email, org, namespace)
        row, was = plan["row"], str(plan["was"])
        carried = await _carry_revoked(plan["revoked"], str(row["owner_email"]), by)
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        if was:  # first, so a link stopped part way leaves no link a re-run can't redo
            await sso_links.unlink(was, [(org, namespace)], by)
        # Insert-as-replace carrying every other column forward.
        await get_client().insert(
            "loom.namespaces",
            [{**row, "contact_email": email, "updated_at": max(now, row["updated_at"])}],
        )
        await sso_links.link(email, [(org, namespace)], by)
    await audit.emit(
        "auth.sso_linked",
        org=org,
        namespace=namespace,
        actor=by,
        subject={
            "email": email,
            "was": was,
            "by": by,
            "subject": row["owner_email"],
            "carried_revoked": carried,
            "elsewhere": bool(plan["elsewhere"]),
        },
    )
    return was, carried, bool(plan["elsewhere"])


async def _carry_revoked(revoked: list[dict[str, Any]], owner: str, by: str) -> list[str]:
    """The subject's own revoked row on each namespace a row under the old address
    named (`link_plan`'s list), unless the subject has a row there already
    (checked again under the grant claims): it caps the person's tokens as the
    address's did. Returns the namespaces carried."""
    from server.auth.grant_claims import grant_change
    from server.core.ch import get_client
    from server.identity.store import grants

    if not revoked:
        return []
    org = revoked[0]["org"]
    names = sorted({g["namespace"] for g in revoked})
    async with AsyncExitStack() as held:
        for ns in names:  # sorted, as grant claims nest
            await held.enter_async_context(grant_change(org, ns, owner))
        rows = await get_client().query_all(
            "SELECT namespace FROM loom.namespace_grants FINAL WHERE org = {org:String} "
            "AND namespace IN {names:Array(String)} AND lower(subject) = lower({owner:String})",
            params={"org": org, "names": names, "owner": owner},
        )
        has = {r["namespace"] for r in rows}
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        carried = {g["namespace"]: g for g in revoked if g["namespace"] not in has}
        await grants.insert_rows(
            [
                {
                    **g,
                    "subject": owner,
                    # Written by the link, not by whoever granted the address.
                    "granted_by": by,
                    "granted_at": now,
                    "revoked_at": now,
                    "version": time.time_ns() + n,
                }
                for n, g in enumerate(carried.values())
            ]
        )
    return sorted(carried)


async def unlink(email: str, org: str = "", *, by: str) -> list[tuple[str, str]]:
    """Stop new SSO sign-ins with `email` reaching its linked accounts (in `org`),
    and the grants stored under `email` admitting them."""
    accounts = [a for a in await sso_links.linked(email) if org in ("", a[0])]
    await sso_links.unlink(email, accounts, by)
    for account_org, namespace in accounts:
        await audit.emit(
            "auth.sso_unlinked",
            org=account_org,
            namespace=namespace,
            actor=by,
            subject={"email": email.strip().lower()},
        )
    return accounts


async def password_link(email: str, org: str = "", *, by: str) -> tuple[str, list[str]]:
    """A one-time code that sets the password of `email`'s SSO accounts, and
    their names. Raises ValueError when there is nothing, or more than one org."""
    from server.identity.app import password_link as links

    accounts = await links.sso_accounts(email, org)
    if not accounts:
        raise ValueError(f"{email} has no SSO account (one without a password)")
    orgs = sorted({a["org"] for a in accounts})
    if len(orgs) > 1:
        raise ValueError(f"{email} has accounts in {orgs}; name the org")
    return await links.make_link(email, accounts, by=by), [a["namespace"] for a in accounts]


# ── bots ──────────────────────────────────────────────────────────────────


def bot_from(
    *,
    name: str,
    org: str,
    namespace_roles: list[str],
    public_key_pem: str = "",
    issuer: str = "",
    audience: str = "",
    claims: list[str] | None = None,
    agent_claim: str = "",
    jwks_uri: str = "",
    max_token_s: int = 3600,
    max_agents: int = 1000,
    max_access_token_s: int = machines.TOKEN_TTL,
    platform_admin: bool = False,
) -> machines.Machine:
    """A bot from what an operator gave; ValueError says what is wrong. With
    `agent_claim`, a trust whose agents each get their own subject and
    namespace (server/auth/agents.py); its namespace roles are what they share.
    `max_access_token_s` caps each token, the hour-long default included. With
    `platform_admin`, every token carries that role, which spans every org: it
    names no namespace and tells no agents apart."""
    from server.auth.authz import GRANT_ROLES

    if not _BOT_NAME.fullmatch(name):
        raise ValueError("name: lower-case letters, digits and '-', at most 63")
    if _SERVICE_ACCOUNT_SUFFIX.fullmatch(name[-7:]):
        raise ValueError("name: may not end in '-' and six hex digits, as service accounts do")
    # One concrete org: never empty, never the `*` a token reads as every org.
    if not org.strip() or "*" in org or org != org.strip():
        raise ValueError(f"org must name one org, got {org!r}")
    ns_roles: dict[str, str] = {}
    for item in namespace_roles:
        ns, sep, role = item.partition("=")
        ns, role = ns.strip(), role.strip().lower()
        if not sep or role not in GRANT_ROLES:
            raise ValueError(f"a namespace role is NS=viewer|writer|admin, got {item!r}")
        # One namespace, never the `*` of all of them; create_bot checks it exists.
        if not ns or "*" in ns or "," in ns or any(c.isspace() for c in ns):
            raise ValueError(f"a namespace role names one namespace, got {item!r}")
        ns_roles[ns] = role
    agent_claim = agent_claim.strip()
    wanted = _wanted_claims(claims or [])
    if platform_admin:
        _check_platform_admin(ns_roles, agent_claim, wanted)
    elif not ns_roles and not agent_claim:
        raise ValueError("a bot needs at least one namespace role")
    issuer = issuer.strip()  # compared exactly with a token's `iss`, slash and all
    trust = bool(issuer or audience or wanted)
    if trust and not (issuer.startswith("https://") and audience and wanted):
        # Any job on the platform can ask for a token with any audience: only
        # claims (which repository, which branch) tell this bot's job apart.
        raise ValueError("a CI trust needs an https issuer, an audience and at least one claim")
    exact = [c for c, v in wanted.items() if c not in _STANDARD_CLAIMS and not v.endswith("*")]
    if trust and not exact:
        # What every token of the issuer carries, or a prefix of it, takes all
        # of them: every tenant's, every job's, into one org.
        raise ValueError(
            "a CI trust needs one claim beyond the standard ones with an exact value "
            "(a repository, a tenant)"
        )
    if not 60 <= max_token_s <= 86400 or max_agents < 1:
        raise ValueError("max_token_s is 60 to 86400 seconds, max_agents at least 1")
    if not machines.MIN_TOKEN_TTL <= max_access_token_s <= machines.MAX_TOKEN_TTL:
        raise ValueError(
            f"max_access_token_s is {machines.MIN_TOKEN_TTL} to {machines.MAX_TOKEN_TTL} seconds"
        )
    if not (trust or public_key_pem):
        raise ValueError("give the bot a public key or a CI trust")
    jwks_uri = jwks_uri.strip()
    if jwks_uri and not (trust and jwks_uri.startswith("https://")):
        raise ValueError("a jwks_uri is the https address of a CI trust's issuer keys")
    if agent_claim and (public_key_pem or not trust):
        raise ValueError("an agent claim needs a workload trust and no key: a key is one bot")
    if agent_claim and f"{name}-".startswith(SSO_NAMESPACE_PREFIX):
        raise ValueError(f"name: its agents' namespaces would take SSO's {SSO_NAMESPACE_PREFIX}")
    if agent_claim:
        # Every agent's token names these and its own namespace (an agent name
        # is at most 63 characters): they must fit before anything is lent.
        used = agents.names_bytes([*ns_roles, f"{name}-" + "x" * 63])
        if used > agents.TOKEN_NAMESPACE_BYTES:
            raise ValueError(
                f"namespace roles: {used} bytes of an agent's token with a 63-character "
                f"agent's own namespace, over {agents.TOKEN_NAMESPACE_BYTES}; share fewer "
                "namespaces, or shorter ones"
            )
    return {
        "name": name,
        "org": org,
        "ns_roles": list(ns_roles.items()),
        "public_keys": machines.public_jwks(public_key_pem.encode()) if public_key_pem else "",
        "issuer": issuer,
        "audience": audience,
        "claims": wanted,
        "agent_claim": agent_claim,
        "jwks_uri": jwks_uri,
        "max_token_s": max_token_s,
        "max_agents": max_agents,
        "max_access_token_s": max_access_token_s,
        "platform_admin": platform_admin,
    }


def _wanted_claims(claims: list[str]) -> dict[str, str]:
    """A trust's `NAME=VALUE` claims; ValueError for one that is not."""
    wanted: dict[str, str] = {}
    for item in claims:
        claim, sep, value = item.partition("=")
        if not sep or not claim.strip() or not value or value == "*":
            raise ValueError(f"a claim is NAME=VALUE (a bare * matches anything), got {item!r}")
        wanted[claim.strip()] = value
    return wanted


def _check_platform_admin(
    ns_roles: dict[str, str], agent_claim: str, wanted: dict[str, str]
) -> None:
    """A platform-admin bot is one identity reaching every namespace, and as a
    trust it takes only the tokens it names exactly."""
    if agent_claim:
        # One identity: an agent's token would carry the role for every agent.
        raise ValueError("a platform-admin bot is one identity: it takes no agent claim")
    if ns_roles:
        raise ValueError("a platform-admin bot reaches every namespace: give it no namespace role")
    if any(v.endswith("*") for v in wanted.values()):
        # A prefix matches tokens of workloads yet to exist: each would hold the role.
        raise ValueError("a platform-admin trust names every claim exactly, no prefix (*)")


async def create_bot(bot: machines.Machine, *, by: str, replace: bool) -> None:
    from server.core.ch import get_client
    from server.identity.store import namespaces

    name, org = bot["name"], bot["org"]
    if bot["max_access_token_s"] != machines.TOKEN_TTL and not (
        await machines.access_token_maximum_ready(get_client())
    ):
        raise ValueError(
            f"a maximum token lifetime other than {machines.TOKEN_TTL} s waits until replicas"
            f" in loom.instances all run migration {machines.ACCESS_TOKEN_MIGRATION} or later"
            " (an older one issues an hour and resets the maximum when it saves the bot)"
        )
    if await revocation.revoked_outright(machines.subject(name)):
        raise ValueError(f"{machines.subject(name)} is revoked outright; it could never sign in")
    async with AsyncExitStack() as held:
        # Each namespace held from its check to the save (in grant_claims'
        # order): a release finishing between them would leave the bot naming
        # a namespace it had dropped from every bot.
        for ns in sorted({ns for ns, _ in bot["ns_roles"]}):
            await held.enter_async_context(account_change(org, ns))
        missing = [ns for ns, _ in bot["ns_roles"] if not await namespaces.is_active(org, ns)]
        if missing:
            raise ValueError(f"no namespace {', '.join(missing)} in org {org!r}; register it first")
        if bot["issuer"]:  # its checks against the other trusts and its save, at once
            await held.enter_async_context(trusts_change())
        await held.enter_async_context(bot_change(name))
        live = await machines.get(name)
        if live is not None and not replace:
            raise ValueError(f"bot {name!r} exists; replace it to change it")
        # A disabled one too: its disable may have saved the row and stopped
        # before its cutoff, so its tokens end here as a replace's do.
        existing = live or await machines.get(name, active_only=False)
        if bot["agent_claim"]:
            await _refuse_overlap(name)
        if bot["issuer"]:
            await _refuse_shared_tokens(bot)
        await machines.save(bot, by=by)
    if existing is not None:
        # Tokens from its old key or wider roles end now, not when they expire.
        # The cutoff is taken after the save, so a request that read the old
        # row falls under it; returning once it has passed lets the new key's
        # first token live.
        await revocation.wait_out(
            await end_tokens(machines.subject(name), by=by, reason="bot replaced")
        )
    try:
        await audit.emit(
            "auth.machine_created",
            org=org,
            namespace="-",
            actor=by,
            subject={
                "name": name,
                "key": bool(bot["public_keys"]),
                "issuer": bot["issuer"],
                "max_access_token_s": bot["max_access_token_s"],
                "platform_admin": bool(bot.get("platform_admin", False)),
            },
        )
    except Exception as e:  # noqa: BLE001 — the bot is saved; its caller must keep the key
        log.warning("bot %s saved, but its audit row was not: %s", name, e)


async def _refuse_overlap(name: str) -> None:
    """Trusts `fleet` and `fleet-a` would both name their agents' namespaces
    `fleet-a-b` (agent `a-b` of one, `b` of the other)."""
    for other in await machines.listed():
        theirs = str(other["name"])
        if theirs != name and other["agent_claim"] and other["active"]:
            if f"{name}-".startswith(f"{theirs}-") or f"{theirs}-".startswith(f"{name}-"):
                raise ValueError(f"name: its agents' namespaces could be those of trust {theirs!r}")


async def _refuse_shared_tokens(bot: machines.Machine) -> None:
    """An exchange naming no client takes the one trust a token meets: two of
    one issuer that can meet one token, one of them telling agents apart, would
    leave that token to neither."""
    for other in await machines.listed():
        if other["name"] == bot["name"] or not other["active"]:
            continue
        # The issuer alone: a token may name several audiences.
        same = other["issuer"] == bot["issuer"]
        agents_either = bool(other["agent_claim"] or bot["agent_claim"])
        if same and agents_either and _could_both_take(bot["claims"], dict(other["claims"])):
            raise ValueError(f"a token this trust takes could also meet {other['name']!r}")


def _could_both_take(a: dict[str, str], b: dict[str, str]) -> bool:
    """Whether one token could meet both trusts' claims: unless a claim both
    name has values no one value matches."""
    for claim in a.keys() & b.keys():
        x, y = a[claim], b[claim]
        if x.endswith("*") and y.endswith("*"):
            fits = x[:-1].startswith(y[:-1]) or y[:-1].startswith(x[:-1])
        elif x.endswith("*") or y.endswith("*"):
            prefix, exact = (x, y) if x.endswith("*") else (y, x)
            fits = exact.startswith(prefix[:-1])
        else:
            fits = x == y
        if not fits:
            return False
    return True


async def disable_bot(name: str, *, by: str) -> bool:
    """Refuse the bot's proofs and revoke the tokens it holds; False if there
    is no such bot. Run again, it finishes a disable that stopped part way."""
    async with bot_change(name):  # a replace running now must not save it active again
        bot = await machines.get(name, active_only=False)
        if bot is None:
            return False
        await machines.save(bot, by=by, active=False)
    # Waited out, as a replace's is, so a bot made again at once is not refused.
    await revocation.wait_out(
        await end_tokens(machines.subject(name), by=by, reason="bot disabled")
    )
    await audit.emit(
        "auth.machine_disabled", org=bot["org"], namespace="-", actor=by, subject={"name": name}
    )
    return True


async def provision_agent(name: str, agent: str, *, by: str) -> tuple[str, str]:
    """Make the namespace of agent `agent` of the trust `name` before its first
    exchange, as that exchange makes it (`agents.ensure_namespace`: the trust's
    tag, the agent as its one owner, the trust's `max_agents`), so grants can
    name the agent's subject first; its first exchange adopts it. Returns the
    org and the namespace. Run again, it changes nothing. Raises ValueError for
    a trust that tells no agents apart, or a namespace the trust refuses."""
    bot = await machines.get(name)
    if bot is None or not bot["agent_claim"]:
        raise ValueError(f"no active bot {name!r} with an agent claim")
    if not agents.AGENT.fullmatch(agent):
        # As its claim must give it: the name is refused, never rewritten.
        raise ValueError("agent: lower-case letters, digits and '-', at most 63")
    try:
        return bot["org"], await agents.ensure_namespace(bot, agent, by=by)
    except machines.Refused as e:
        raise ValueError(e.description or e.error) from None


async def end_tokens(subject: str, *, by: str, reason: str) -> datetime:
    """Revoke existing bot or agent tokens with a permanent issue-time cutoff."""
    cutoff = revocation.from_now_on()
    await revocation.cut_off(subject, issued_before=cutoff, reason=reason, revoked_by=by)
    return cutoff
