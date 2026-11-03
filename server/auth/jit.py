"""Just-in-time accounts for OIDC sign-in: a verified email with no account
in `oidc_jit_org` gets a personal namespace, and an existing one is never
rebuilt (that would hand back its data and an admin grant)."""

from __future__ import annotations

import enum
import hashlib
import itertools
import logging
import secrets
import time
from typing import Any

from server.auth import login
from server.core import leases
from server.core.ch import get_client
from server.core.naming import SSO_NAMESPACE_PREFIX

log = logging.getLogger(__name__)


async def _registry_row(org: str, namespace: str) -> dict[str, Any] | None:
    """The registry row of `namespace`, active or not, read as of every write
    that landed (a concurrent first sign-in may have just written it)."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        row = await get_client().query_one(
            """
            SELECT active, toString(contact_email) AS contact_email, password_hash = '' AS sso
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    return None if row is None else dict(row)


async def _accounts_naming(org: str, ident: str) -> list[dict[str, Any]]:
    """Every registry row in `org` whose contact is `ident`, active or not
    (deleted ones keep it; a released one has it cleared), read as of every
    write that landed."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        rows = await get_client().query_all(
            """
            SELECT namespace, toString(owner_email) AS owner, active, tags,
                   password_hash = '' AS sso
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND contact_email = {ident:String}
            """,
            params={"org": org, "ident": ident},
        )
    return [dict(r) for r in rows]


# Marks an account JIT provisioned, so a sign-in can finish one whose owner
# grant never landed.
JIT_TAG = "oidc-jit"


async def _finish_provisioning(org: str, accounts: list[dict[str, Any]]) -> None:
    """Write the owner grant of an active JIT account that never got one: its
    provisioning wrote the namespace row and failed before the grant. A grant
    revoked or lowered since leaves a row, so it is never given back here."""
    from server.auth.self_signup import insert_owner_grant

    for a in accounts:
        if a["active"] and JIT_TAG in a["tags"] and not await _ever_granted(org, a):
            log.info("oidc jit: finishing %s/%s, whose owner grant never landed", org, a["owner"])
            await insert_owner_grant(org, a["namespace"], a["owner"])


async def _ever_granted(org: str, account: dict[str, Any]) -> bool:
    """Whether any grant row, live, revoked or pending, names the owner."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        row = await get_client().query_one(
            """
            SELECT 1 AS x FROM loom.namespace_grants
            WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}
            LIMIT 1
            """,
            params={"org": org, "ns": account["namespace"], "sub": account["owner"]},
        )
    return row is not None


# The names a JIT sign-up tries past the first: `<name>-2` to `<name>-6`, then
# random suffixes.
_JIT_SUFFIX_TRIES = 5
_JIT_RANDOM_TRIES = 5
# How long a sign-in waits for another sign-up of the same email to finish.
_JIT_WAIT_S = 10.0


class _Name(enum.Enum):
    """What the walk over a JIT sign-up's names found."""

    FREE = enum.auto()  # a name for the new account
    THEIRS = enum.auto()  # the person's account, which a first sign-in elsewhere just wrote
    NONE_FREE = enum.auto()  # every name tried is taken


async def jit_provision_for_email(email: str) -> login.Identity | None:
    """Just-in-time provisioning for OIDC: when enabled, a verified email with
    no loom account gets a personal namespace auto-created (in oidc_jit_org),
    then resolves like any other account. Returns None (the caller then
    rejects the login) when JIT is off or the email's domain isn't allowed,
    when an account of the person's there is revoked outright, when their
    active account there does not resolve (the cross-org guard), or when no
    free name is found.
    """
    from server.auth.self_signup import CLAIM_TTL_S, email_claim
    from server.settings import settings as _settings

    if not _settings.oidc_jit_provision:
        return None
    ident = email.strip().lower()
    if "@" not in ident:
        return None
    domain = ident.rsplit("@", 1)[1]
    allowed = _settings.oidc_jit_email_domains_set
    if allowed and domain not in allowed:
        return None
    # Deterministic, unique-per-email namespace: a readable slug + an email
    # hash so two different emails can never collide onto one account.
    local = ident.split("@", 1)[0]
    slug = "".join(c if c.isalnum() else "-" for c in local).strip("-")[:40] or "user"
    base = f"{SSO_NAMESPACE_PREFIX}{slug}-{hashlib.sha256(ident.encode()).hexdigest()[:8]}"
    org = _settings.oidc_jit_org
    # One sign-up per email at a time on any replica, under self-signup's claim of
    # the email: a second sign-in waits and finds the account the first wrote.
    try:
        async with leases.holding(
            email_claim(org, ident), ttl_s=CLAIM_TTL_S, wait_s=_JIT_WAIT_S
        ) as held:
            if not held:
                log.warning("oidc jit: sign-up of %r in %s still running elsewhere", ident, org)
                raise login.SignInUnavailable
            return await _provision_jit(org, ident, base)
    except login.SignInUnavailable:
        raise
    except Exception as e:  # a read or write that failed or ran out of time: retryable
        log.error("oidc jit: sign-up of %r in %s failed: %s", ident, org, e, exc_info=True)
        raise login.SignInUnavailable from e


async def finish_unfinished_account(email: str) -> None:
    """Before an OIDC sign-in resolves `email`: write the owner grant of its
    JIT account if that account's provisioning failed before the grant. A
    no-op when JIT is off. Raises `SignInUnavailable` when it cannot read."""
    from server.settings import settings as _settings

    if not _settings.oidc_jit_provision:
        return
    org, ident = _settings.oidc_jit_org, email.strip().lower()
    try:
        await _finish_provisioning(org, await _accounts_naming(org, ident))
    except Exception as e:  # a read or write that failed or ran out of time: retryable
        log.error("oidc jit: finishing %r in %s failed: %s", ident, org, e, exc_info=True)
        raise login.SignInUnavailable from e


async def _provision_jit(org: str, ident: str, base: str) -> login.Identity | None:
    """`jit_provision_for_email` past its gates, under the email's claim."""
    from server.auth.self_signup import provision_account
    from server.observability import audit

    accounts = await _accounts_naming(org, ident)
    # A password account naming the email is no proof the person owns it
    # (self-signup never verified the address), so it is not theirs here.
    if any(a["active"] and a["sso"] for a in accounts):
        return await _resolve_as_written(ident)  # their account, whatever its name
    # Nor is it someone else's to sit beside: with two active accounts naming
    # one email, its sign-in would have two to choose from, and a revocation
    # of the address reaches both (`aliases.spellings`). The address is freed
    # by releasing that account (`loom claim-release`).
    if any(a["active"] for a in accounts):
        log.info("oidc jit: a password account names %r in %s; not provisioning", ident, org)
        return None
    # Any account naming the email counts, a password one too: its owner may
    # be the person offboarded. A squatter's is released (`loom claim-release`
    # clears its email), not revoked, to free the address.
    if await _any_revoked(accounts):
        # Offboarded: a fresh name would be a new subject past the revoke.
        log.info("oidc jit: an account of %r in %s is revoked; not provisioning", ident, org)
        return None
    found, name = await _free_name(org, ident, base)
    if found is _Name.THEIRS:
        return await _resolve_as_written(ident)
    if found is _Name.NONE_FREE:
        log.warning("oidc jit: no free name for %r in %s", ident, org)
        return None
    await provision_account(
        org=org,
        namespace=name,
        subject=name,
        contact_email=ident,
        password_hash="",  # SSO-only account
        description="Auto-provisioned via OIDC SSO.",
        tags=[JIT_TAG],
    )
    await audit.emit(
        "auth.jit_provisioned",
        org=org,
        namespace=name,
        actor=ident,
        subject={"provider": "oidc", "namespace": name},
    )
    # Two processes that both found the name free write the same row, and
    # ReplacingMergeTree keeps one.
    return await _resolve_as_written(ident)


async def _free_name(org: str, ident: str, base: str) -> tuple[_Name, str]:
    """The first name free for a new account: `base`, `base-2` and on, then
    random suffixes, so taking every predictable name cannot lock the person
    out. A taken name is never rebuilt: that would hand back its data and an
    admin grant."""
    predictable = [base, *(f"{base}-{n}" for n in range(2, _JIT_SUFFIX_TRIES + 2))]
    random_names = (f"{base}-{secrets.token_hex(4)}" for _ in range(_JIT_RANDOM_TRIES))
    for ns in itertools.chain(predictable, random_names):
        row = await _registry_row(org, ns)
        if row is None:
            return _Name.FREE, ns
        if row["active"] and row["sso"] and row["contact_email"] == ident:
            return _Name.THEIRS, ns
    return _Name.NONE_FREE, ""


async def _any_revoked(accounts: list[dict[str, Any]]) -> bool:
    """Whether the subject of any of `accounts` is revoked outright."""
    from server.auth.revocation import subjects_revoked

    subjects = {a["namespace"] for a in accounts} | {a["owner"] for a in accounts}
    return await subjects_revoked(sorted(subjects - {""}), issued_at=int(time.time()))


async def _resolve_as_written(ident: str) -> login.Identity | None:
    """`resolve_identity_for_email`, reading every write that landed."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        return await login.resolve_identity_for_email(ident)
