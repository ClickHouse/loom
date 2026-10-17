"""What an operator does to accounts and bots, shared by the host CLI
(`loom sso`, `loom bot`) and the platform-admin API (`/v1/admin/sso.*`,
`/v1/admin/bots.*`), so the two cannot drift."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

from server.auth import machines, revocation, sso_links
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

# How far another replica's clock may run ahead of this one's: a token it
# issues as of a later second than the cutoff would outlive the revocation.
_CLOCK_SKEW_S = 5


def _from_now_on() -> datetime:
    """A cutoff past every token issued so far, on any replica: revocation
    compares whole seconds, and the token's `iat` is another clock's."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1 + _CLOCK_SKEW_S)


def _after_this_second() -> datetime:
    """A cutoff that ends the tokens issued so far and none after: a person's,
    whose next token comes from a new sign-in, not from a row that may race."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)


async def _wait_until(cutoff: datetime) -> None:
    await asyncio.sleep(max(0.0, cutoff.timestamp() - time.time()))


# ── carrying password accounts over to SSO ────────────────────────────────


async def adopt_plan(domain: str, org: str = "", skip: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Every unlinked password account at `domain`, with why it would be
    skipped ('' to adopt). `skip` names emails the operator leaves out, such
    as a bot's: no person signs in as it, and adopting it would revoke the
    tokens it runs on."""
    left_out = {e.strip().lower() for e in skip}
    rows = await sso_links.unlinked_in_domain(domain, org)
    by_email: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_email.setdefault(r["contact_email"], []).append(r)
    on_sso = await sso_links.reached_by_sso(list(by_email))
    return [
        {
            "email": email,
            "org": r["org"],
            "namespace": r["namespace"],
            "owner": r["owner_email"],
            "created_at": str(r["created_at"])[:10],
            "skip": "left out (--skip)"
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
    for email, accounts in adoptable.items():
        org, owner = accounts[0]["org"], accounts[0]["owner"]
        # Whoever registered the address may hold tokens from before; the person
        # signs in again through the provider. Revoked before the link lands, so
        # an adopt stopped between the two is redone whole by the next one.
        if not keep_tokens:
            await revocation.cut_off(
                owner, issued_before=_after_this_second(), reason="sso adopt", revoked_by=by
            )
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
    return linked


async def unlink(email: str, org: str = "", *, by: str) -> list[tuple[str, str]]:
    """Stop new SSO sign-ins with `email` reaching its linked accounts (in `org`)."""
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
) -> machines.Machine:
    """A bot from what an operator gave; ValueError says what is wrong. With
    `agent_claim`, a trust whose agents each get their own subject and
    namespace (server/auth/agents.py); its namespace roles are what they share."""
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
    if not ns_roles and not agent_claim:
        raise ValueError("a bot needs at least one namespace role")
    wanted: dict[str, str] = {}
    for item in claims or []:
        claim, sep, value = item.partition("=")
        if not sep or not claim.strip() or not value or value == "*":
            raise ValueError(f"a claim is NAME=VALUE (a bare * matches anything), got {item!r}")
        wanted[claim.strip()] = value
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
    if not (trust or public_key_pem):
        raise ValueError("give the bot a public key or a CI trust")
    jwks_uri = jwks_uri.strip()
    if jwks_uri and not (trust and jwks_uri.startswith("https://")):
        raise ValueError("a jwks_uri is the https address of a CI trust's issuer keys")
    if agent_claim and (public_key_pem or not trust):
        raise ValueError("an agent claim needs a workload trust and no key: a key is one bot")
    if agent_claim and f"{name}-".startswith(SSO_NAMESPACE_PREFIX):
        raise ValueError(f"name: its agents' namespaces would take SSO's {SSO_NAMESPACE_PREFIX}")
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
    }


async def create_bot(bot: machines.Machine, *, by: str, replace: bool) -> None:
    from server.identity.store import namespaces

    name, org = bot["name"], bot["org"]
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
        existing = await machines.get(name)
        if existing is not None and not replace:
            raise ValueError(f"bot {name!r} exists; replace it to change it")
        if bot["agent_claim"]:
            await _refuse_overlap(name)
        if bot["issuer"]:
            await _refuse_shared_tokens(bot)
        await machines.save(bot, by=by)
    if existing is not None:
        # Tokens from its old key or wider roles end now, not within the hour.
        # The cutoff is taken after the save, so a request that read the old
        # row falls under it; returning once it has passed lets the new key's
        # first token live.
        await _wait_until(await end_tokens(machines.subject(name), by=by, reason="bot replaced"))
    try:
        await audit.emit(
            "auth.machine_created",
            org=org,
            namespace="-",
            actor=by,
            subject={"name": name, "key": bool(bot["public_keys"]), "issuer": bot["issuer"]},
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
    await _wait_until(await end_tokens(machines.subject(name), by=by, reason="bot disabled"))
    await audit.emit(
        "auth.machine_disabled", org=bot["org"], namespace="-", actor=by, subject={"name": name}
    )
    return True


async def end_tokens(subject: str, *, by: str, reason: str) -> datetime:
    """A bot's or an agent's tokens live an hour; revoked now, not when they
    run out. Returns the cutoff."""
    cutoff = _from_now_on()
    await revocation.cut_off(subject, issued_before=cutoff, reason=reason, revoked_by=by)
    return cutoff
