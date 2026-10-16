"""What an operator does to accounts and bots, shared by the host CLI
(`loom sso`, `loom bot`) and the platform-admin API (`/v1/admin/sso.*`,
`/v1/admin/bots.*`), so the two cannot drift."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from server.auth import machines, revocation, sso_links
from server.observability import audit

log = logging.getLogger(__name__)

_BOT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


# How far another replica's clock may run ahead of this one's: a token it
# issues as of a later second than the cutoff would outlive the revocation.
_CLOCK_SKEW_S = 5


def _from_now_on() -> datetime:
    """A cutoff past every token issued so far, on any replica: revocation
    compares whole seconds, and the token's `iat` is another clock's."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1 + _CLOCK_SKEW_S)


async def _wait_until(cutoff: datetime) -> None:
    await asyncio.sleep(max(0.0, cutoff.timestamp() - time.time()))


# ── carrying password accounts over to SSO ────────────────────────────────


async def adopt_plan(domain: str, org: str = "") -> list[dict[str, Any]]:
    """Every unlinked password account at `domain`, with why it would be
    skipped ('' to adopt)."""
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
            "skip": _skip_reason(accounts, email in on_sso),
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
    """Link the plan's adoptable accounts; returns how many."""
    adoptable: dict[str, list[dict[str, Any]]] = {}
    for a in plan:
        if not a["skip"]:
            adoptable.setdefault(a["email"], []).append(a)
    for email, accounts in adoptable.items():
        org, owner = accounts[0]["org"], accounts[0]["owner"]
        # Whoever registered the address may hold tokens from before; the person
        # signs in again through the provider. Revoked before the link lands, so
        # an adopt stopped between the two is redone whole by the next one. A
        # cutoff written over an outright revoke would let the offboarded
        # subject back in, so that one stands.
        if not keep_tokens and not await revocation.revoked_outright(owner):
            await revocation.revoke(
                owner, issued_before=_from_now_on(), reason="sso adopt", revoked_by=by
            )
        await sso_links.link(email, [(org, a["namespace"]) for a in accounts], by)
        await audit.emit(
            "auth.sso_linked",
            org=org,
            namespace="-",
            actor=by,
            subject={
                "email": email,
                "by": by,
                "subject": owner,
                "namespaces": [a["namespace"] for a in accounts],
                "tokens_revoked": not keep_tokens,
            },
        )
    return sum(len(a) for a in adoptable.values())


async def unlink(email: str, *, by: str) -> list[tuple[str, str]]:
    """Stop new SSO sign-ins with `email` reaching its linked accounts."""
    accounts = await sso_links.linked(email)
    await sso_links.unlink(email, accounts, by)
    for org, namespace in accounts:
        await audit.emit(
            "auth.sso_unlinked",
            org=org,
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
) -> machines.Machine:
    """A bot from what an operator gave; ValueError says what is wrong."""
    from server.auth.authz import GRANT_ROLES

    if not _BOT_NAME.fullmatch(name):
        raise ValueError("name: lower-case letters, digits and '-', at most 63")
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
    if not ns_roles:
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
    if not (trust or public_key_pem):
        raise ValueError("give the bot a public key or a CI trust")
    return {
        "name": name,
        "org": org,
        "ns_roles": list(ns_roles.items()),
        "public_keys": machines.public_jwks(public_key_pem.encode()) if public_key_pem else "",
        "issuer": issuer,
        "audience": audience,
        "claims": wanted,
    }


async def create_bot(bot: machines.Machine, *, by: str, replace: bool) -> None:
    from server.core.ch import own_write_reads
    from server.identity.store import namespaces

    name, org = bot["name"], bot["org"]
    with own_write_reads():  # "register it, then the bot" must not read a replica behind
        missing = [ns for ns, _ in bot["ns_roles"] if not await namespaces.is_active(org, ns)]
    if missing:
        raise ValueError(f"no namespace {', '.join(missing)} in org {org!r}; register it first")
    if await revocation.revoked_outright(machines.subject(name)):
        raise ValueError(f"{machines.subject(name)} is revoked outright; it could never sign in")
    existing = await machines.get(name)
    if existing is not None and not replace:
        raise ValueError(f"bot {name!r} exists; replace it to change it")
    await machines.save(bot, by=by)
    if existing is not None:
        # Tokens from its old key or wider roles end now, not within the hour.
        # The cutoff is taken after the save, so a request that read the old
        # row falls under it; returning once it has passed lets the new key's
        # first token live.
        await _wait_until(await _end_tokens(name, by=by, reason="bot replaced"))
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


async def disable_bot(name: str, *, by: str) -> bool:
    """Refuse the bot's proofs and revoke the tokens it holds; False if none is active."""
    bot = await machines.get(name)
    if bot is None:
        return False
    await machines.save(bot, by=by, active=False)
    await _end_tokens(name, by=by, reason="bot disabled")
    await audit.emit(
        "auth.machine_disabled", org=bot["org"], namespace="-", actor=by, subject={"name": name}
    )
    return True


async def _end_tokens(name: str, *, by: str, reason: str) -> datetime:
    """Its tokens live an hour; revoked now, not when they run out (unless
    revoked outright already, which a cutoff must not weaken). Returns the cutoff."""
    subject, cutoff = machines.subject(name), _from_now_on()
    if not await revocation.revoked_outright(subject):
        await revocation.revoke(subject, issued_before=cutoff, reason=reason, revoked_by=by)
    return cutoff
