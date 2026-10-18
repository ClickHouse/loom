"""Who an identity provider has deactivated over SCIM 2.0 (`loom.scim_users`).

A SCIM user is an email the provider manages: its `emails` entry marked
primary, else its work entry, else its first, else its userName
(`scim_routes.user_name_and_email`). Loom makes no account for one:
JIT makes that at the person's first sign-in, as without SCIM. What SCIM
changes is `active`:

- Deactivating revokes outright every subject an SSO sign-in with the email
  could reach (an SSO account naming it, or a password account linked to it),
  in every org: every session and every token those subjects made, since a
  delegated token names them as its creator or a parent; the tokens they saved
  read revoked in the Tokens list. SSO sign-in with the email is refused while
  it lasts, even when it reaches no account yet.
- Reactivating lifts the refusal and narrows those revocations to the tokens
  issued before now, so the person signs in again and the old tokens stay dead.
  Every request that leaves a user active does it (`_converge_active`), so a
  repeat of one finishes what a failed request left.

SCIM's revocations are its own: each is stored under the subject's SCIM key
(`revocation.SCIM_PREFIX`), and SCIM never reads or writes the row an
operator's `loom revoke-token` writes. The two are independent, and either
keeps a person out; a reactivation lifts only SCIM's. A subject another
deactivated user still reaches stays revoked through a reactivation. The
accounts and their data stay: this is not erasure.
Leases, always taken in this order: the user's, then a userName's it takes
(create, rename), then, around a deactivation's revocations or a reactivation's
lifts and the row they belong with, one per subject the user reaches, in sorted
order: only users sharing a subject wait on each other. Each step is
idempotent, so a provider's retry of a half-done one finishes it. With
`LOOM_SCIM_TOKEN` empty nothing here is read.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from server.auth import login, revocation, service_accounts
from server.core import leases
from server.core.ch import get_client, own_write_reads
from server.core.versions import next_version
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)

TABLE = "loom.scim_users"
COLUMNS = (
    "id",
    "user_name",
    "email",
    "email_type",
    "email_primary",
    "active",
    "deleted",
    "subjects",
    "created_at",
    "updated_at",
    "version",
)
# The reasons on the revocations a deactivation and a reactivation write.
DEACTIVATED = "scim: deactivated"
REACTIVATED = "scim: reactivated"
_BY = "scim"
# SCIM ids are uuid5 of the email under this namespace: one id per person,
# whichever replica or retry computes it. The provider holds the email anyway.
_ID_NAMESPACE = uuid.UUID("6f1c3e0a-5b7d-4c2e-9a41-2d8f0b6e7c13")
_LEASE_TTL_S = 60.0
_LEASE_WAIT_S = 10.0


class Exists(Exception):
    """A live SCIM user already has this email or userName."""


class Busy(Exception):
    """Another request is changing this user; retry shortly."""


def normalized(address: str) -> str:
    return address.strip().lower()


def user_id(email: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, normalized(email)))


async def get(uid: str) -> dict[str, Any] | None:
    """The SCIM user `uid`, unless absent or deleted."""
    row = await _row(uid)
    return None if row is None or row["deleted"] else row


async def find(user_name: str) -> dict[str, Any] | None:
    """The live SCIM user whose userName is `user_name`."""
    row = await get_client().query_one(
        f"SELECT * FROM {TABLE} FINAL WHERE user_name = {{n:String}} AND deleted = 0 LIMIT 1",
        params={"n": normalized(user_name)},
    )
    return None if row is None else dict(row)


async def page(start: int, count: int) -> tuple[int, list[dict[str, Any]]]:
    """The total of live SCIM users and those from `start` (1-based), at most `count`."""
    ch = get_client()
    total = await ch.query_one(f"SELECT count() AS n FROM {TABLE} FINAL WHERE deleted = 0")
    rows = await ch.query_all(
        f"""
        SELECT * FROM {TABLE} FINAL WHERE deleted = 0
        ORDER BY user_name LIMIT {{count:UInt32}} OFFSET {{skip:UInt32}}
        """,
        params={"count": count, "skip": max(0, start - 1)},
    )
    return int((total or {}).get("n") or 0), [dict(r) for r in rows]


async def deactivated(email: str) -> bool:
    """Whether SCIM has deactivated `email`, deleted users included; never
    while SCIM is off. Raises `SignInUnavailable` when it cannot read: a
    sign-in must not pass unchecked."""
    if not settings.scim_token:
        return False
    try:
        with own_write_reads():  # a deactivation may have landed a moment ago elsewhere
            row = await get_client().query_one(
                f"SELECT 1 AS x FROM {TABLE} FINAL WHERE id = {{id:String}} AND active = 0",
                params={"id": user_id(email)},
            )
    except Exception as e:  # a read that failed or ran out of time: retryable
        raise login.SignInUnavailable from e
    return row is not None


async def create(
    user_name: str, email: str, *, active: bool, email_entry: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Record the SCIM user for `email`. Raises Exists when one is live,
    once a retried re-create has waited out its cutoff. A deleted one comes
    back, still deactivated unless `active`. `email_entry` is the type and
    primary flag of the entry the email came from."""
    uid = user_id(email)
    async with _held(_user_lease(uid)), _held(_name_lease(user_name)):
        row = await _row(uid)
        exists = row is not None and not row["deleted"]
        if row is None:
            row = {
                "id": uid,
                "email": normalized(email),
                "email_type": "",
                "email_primary": 0,
                "active": 1,
                "deleted": 0,
                "subjects": [],
                "created_at": datetime.now(UTC),
            }
        cutoff = None
        if exists:
            # A provider retrying a create that failed or timed out finishes it,
            # waits out what is left of its cutoff, then gets its 409.
            if bool(row["active"]) == active:
                row, cutoff = await _set_active(row, active)
        else:
            await _refuse_taken(user_name, uid)
            changes = {"user_name": normalized(user_name), "deleted": 0, **_entry(email_entry)}
            row, cutoff = await _set_active(row, active, **changes)
    if not row["active"]:  # a retry too: it finishes what a failed try left
        await _revoke_tokens(row)
    await _wait_out(cutoff)
    if exists:
        raise Exists
    return row


async def set_active(uid: str, active: bool) -> dict[str, Any] | None:
    """Deactivate or reactivate `uid`; None when there is no such user."""
    return await update(uid, active=active)


async def update(
    uid: str,
    *,
    active: bool | None = None,
    user_name: str | None = None,
    email_entry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Set `active`, rename the userName (it is not the key) and/or note the
    type and primary flag of the email's entry; None when there is no such user."""
    async with _held(_user_lease(uid)):
        row = await get(uid)
        if row is None:
            return None
        changes = _entry(email_entry)
        if not user_name or normalized(user_name) == row["user_name"]:
            new, cutoff = await _set_active(row, active, **changes)
        else:
            async with _held(_name_lease(user_name)):
                await _refuse_taken(user_name, uid)
                renamed = normalized(user_name)
                new, cutoff = await _set_active(row, active, user_name=renamed, **changes)
    if active is False:  # a retry too: it finishes what a failed try left
        await _revoke_tokens(new)
    await _wait_out(cutoff)
    return new


def _entry(email_entry: dict[str, Any] | None) -> dict[str, Any]:
    if email_entry is None:
        return {}
    return {
        "email_type": str(email_entry.get("type") or "").lower(),
        "email_primary": int(is_primary(email_entry.get("primary"))),
    }


def is_primary(value: Any) -> bool:
    """A SCIM `primary` flag as providers send it (true, "True") or Loom stores it (1)."""
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True or value == 1


async def delete(uid: str) -> bool:
    """A SCIM DELETE: the user is deactivated and answers 404 from now on.
    A retry of one whose token step failed finishes it, and still answers 404."""
    async with _held(_user_lease(uid)):
        row = await _row(uid)
        if row is None:
            return False
        deleted = bool(row["deleted"])
        row, _ = await _set_active(row, False, deleted=1)  # a retry finishes a failed try
    await _revoke_tokens(row)
    return not deleted


async def _refuse_taken(user_name: str, uid: str) -> None:
    """Raise Exists when another live user holds `user_name`: a filter must
    find one. Run under the userName's lease, so two creates cannot both pass."""
    with own_write_reads():
        other = await find(user_name)
    if other is not None and other["id"] != uid:
        raise Exists


def _user_lease(uid: str) -> str:
    return f"scim-user:{uid}"


def _name_lease(user_name: str) -> str:
    # Hashed like the id: the lease store is no place for an address.
    return f"scim-username:{user_id(user_name)}"


@asynccontextmanager
async def _held(name: str) -> AsyncIterator[None]:
    try:
        async with leases.holding(name, ttl_s=_LEASE_TTL_S, wait_s=_LEASE_WAIT_S) as held:
            if not held:
                raise Busy
            yield
    except leases.LeaseUnavailable as e:  # the store did not answer, or the work ran long
        raise Busy from e


@asynccontextmanager
async def _subject_leases(subjects: list[str]) -> AsyncIterator[None]:
    """One lease per subject, in sorted order, so users sharing one never
    interleave their revocations and two never wait on each other in a cycle."""
    async with AsyncExitStack() as held:
        for subject in sorted(set(subjects)):
            # Keyed like the audit's: the lease store is no place for an address.
            await held.enter_async_context(_held(f"scim-subject:{_keyed(subject)[:32]}"))
        yield


async def _row(uid: str) -> dict[str, Any] | None:
    with own_write_reads():
        row = await get_client().query_one(
            f"SELECT * FROM {TABLE} FINAL WHERE id = {{id:String}}", params={"id": uid}
        )
    return None if row is None else dict(row)


async def _revoke_tokens(row: dict[str, Any]) -> None:
    # Past the leases, as it may claim many token names. It lists revoked the
    # tokens the revocations kill and frees their names; one made after a
    # reactivation's or an operator's cutoff stays live.
    await service_accounts.revoke_created_by(row["subjects"], revoked_by=_BY)


async def _wait_out(cutoff: datetime | None) -> None:
    # Past the leases, so a deactivation sent meanwhile does not wait on it and
    # turn Busy; until every replica is past it, a token issued before stays revoked.
    if cutoff is not None:
        await revocation.wait_out(cutoff)


async def _set_active(
    row: dict[str, Any], active: bool | None, **changes: Any
) -> tuple[dict[str, Any], datetime | None]:
    """The row as written, and a reactivation's cutoff for the caller to wait
    out once it lets its leases go. `active` None leaves it as it is."""
    if active is None and row["active"]:  # nothing asked of an active user: no subject
        new = {**row, **changes}
        await _write_changed(row, new)
        return new, None
    if active:
        return await _converge_active(row, **changes)
    new = {**row, **changes, "active": 0}
    found = await _subjects(row["email"])
    known = set(row["subjects"])
    async with _subject_leases([*known, *found]):
        new["subjects"] = sorted(known | set(found))
        # Going inactive, or a try that stopped short of writing the row, revokes
        # them all. Once inactive, a repeat revokes the accounts found since: one
        # an operator narrowed (someone else's subject, given back) stays narrowed.
        if row["active"] or "version" not in row:
            subjects = new["subjects"]
        else:
            subjects = sorted(set(found) - known)
        for subject in subjects:
            await revocation.scim_revoke(subject, reason=DEACTIVATED, revoked_by=_BY, reread=False)
            await _flag_collateral(new["id"], subject, set(found.values()))
        await _write_changed(row, new)
        # Audited as the row lands: a retry after a later failure finds the row
        # already changed and would never audit the transition.
        await _audit_transition(row, new)
    # The snapshot re-read each revocation would make, once and past the leases.
    await revocation.refresh()
    if set(await _subjects(row["email"])) - set(new["subjects"]):
        # An account made before the row said inactive (a first sign-in's JIT):
        # revoke it too, with the sessions and tokens it made meanwhile.
        new, _ = await _set_active(new, False)
    return new, None


async def _converge_active(
    row: dict[str, Any], **changes: Any
) -> tuple[dict[str, Any], datetime | None]:
    """The only way a user ends active: write the row, then lift what SCIM still
    revokes outright of every subject the email reaches, whatever a failed
    request left. The cutoff is the newest SCIM narrowed them to, for the
    caller to wait out (a retry waits out the rest)."""
    new = {**row, **changes, "active": 1}
    subjects = sorted(set(row["subjects"]) | set(await _subjects(row["email"])))
    rows = await revocation.scim_revocations(subjects)
    if not _outright(rows):  # nothing to lift, as on most pushes: no subject lease
        await _write_changed(row, new)
        await _audit_transition(row, new)
        return new, _newest_cutoff(rows)
    async with _subject_leases(subjects):
        # The row first: a lift that fails leaves the user active and still
        # revoked, never inactive with a subject let go. A retry lifts the rest.
        await _write_changed(row, new)
        await _audit_transition(row, new)
        cutoff = await _lift(row, subjects)
    await revocation.refresh()
    return new, cutoff


async def _write_changed(row: dict[str, Any], new: dict[str, Any]) -> None:
    if new != row or "version" not in row:  # a change, or a user not yet written
        await _write(new)


async def _audit_transition(row: dict[str, Any], new: dict[str, Any]) -> None:
    if bool(new["active"]) != bool(row["active"]):
        await _audit("auth.scim_reactivated" if new["active"] else "auth.scim_deactivated", new)
    if new["deleted"] and not row["deleted"]:
        await _audit("auth.scim_deleted", new)


async def _subjects(email: str) -> dict[str, str]:
    """Every subject an SSO sign-in with `email` could reach, in any org, and
    its org. Unlike sign-in, several are no reason to stop: each is revoked."""
    with own_write_reads():
        rows = await get_client().query_all(
            """
            SELECT DISTINCT owner_email, org FROM loom.namespaces FINAL
            WHERE contact_email = {email:String} AND active = 1 AND owner_email != ''
              AND (password_hash = '' OR (org, namespace) IN (
                  SELECT org, namespace FROM loom.sso_links FINAL
                  WHERE email = {email:String} AND linked = 1
              ))
            """,
            params={"email": email},
        )
    return {r["owner_email"]: r["org"] for r in rows}


async def _flag_collateral(uid: str, subject: str, orgs: set[str]) -> None:
    """Log and audit `subject`, just revoked, when it also holds accounts or
    grants beyond `orgs`: a subject is a name, not an org's, and the same one
    may be someone else's elsewhere."""
    elsewhere = await _orgs_beyond(subject, orgs)
    if not elsewhere:
        return
    # Failing closed, it stays revoked; an operator sees it and can give it back.
    log.warning(
        "scim: deactivating %s revokes a subject that also holds accounts or grants "
        "in %s, which this email does not reach (see `loom revoke-token --list`)",
        uid,
        ", ".join(elsewhere),
    )
    await audit.emit(
        "auth.scim_collateral",
        org="-",
        namespace="-",
        actor=_BY,
        subject={"id": uid, "subject": _keyed(subject), "orgs": elsewhere},
    )


async def _orgs_beyond(subject: str, orgs: set[str]) -> list[str]:
    """The orgs other than `orgs` where `subject` owns an account or holds a grant."""
    from server.core.grant_sql import LIVE_GRANT

    rows = await get_client().query_all(
        f"""
        SELECT DISTINCT org FROM (
            SELECT org FROM loom.namespaces FINAL WHERE owner_email = {{s:String}} AND active = 1
            UNION ALL
            SELECT org FROM loom.namespace_grants FINAL
            WHERE lower(subject) = lower({{s:String}}) AND {LIVE_GRANT}
        )
        WHERE org NOT IN {{orgs:Array(String)}}
        ORDER BY org
        """,
        params={"s": subject, "orgs": sorted(orgs)},
    )
    return [r["org"] for r in rows]


async def _lift(row: dict[str, Any], subjects: list[str]) -> datetime | None:
    """Narrow SCIM's outright revocations of `subjects` to the tokens issued so
    far; the newest cutoff of its narrowed ones, also when a try this one
    retries narrowed them. A subject another deactivated (or deleted) user also
    reaches stays revoked: lifting it would let that one back in."""
    # No skew ahead: sign-in is refused while deactivated, so only a replica behind matters.
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    rows = await revocation.scim_revocations(subjects)  # again, under the leases
    lifted = False
    for subject in sorted(_outright(rows)):
        if await _held_by_another_inactive(row["id"], subject):
            if not row["active"]:  # said as the user comes back, not on every push
                log.warning(
                    "scim: reactivating %s leaves a subject revoked that another "
                    "deactivated user still reaches",
                    row["id"],
                )
            continue
        await revocation.scim_revoke(
            subject, issued_before=cutoff, reason=REACTIVATED, revoked_by=_BY, reread=False
        )
        lifted = True
    return cutoff if lifted else _newest_cutoff(rows)


def _outright(rows: dict[str, dict[str, Any]]) -> set[str]:
    """The subjects SCIM revokes outright: an operator's `--scim` row is not its own."""
    return {s for s, r in rows.items() if r["issued_before"] is None and r["revoked_by"] == _BY}


def _newest_cutoff(rows: dict[str, dict[str, Any]]) -> datetime | None:
    """The newest cutoff a reactivation narrowed SCIM's revocations to; an
    operator's (`--scim`) is never waited out."""
    cutoffs: list[datetime] = [
        r["issued_before"] for r in rows.values() if r["issued_before"] and r["revoked_by"] == _BY
    ]
    if not cutoffs:
        return None
    cutoff = max(cutoffs)
    return cutoff if cutoff.tzinfo is not None else cutoff.replace(tzinfo=UTC)


async def _held_by_another_inactive(uid: str, subject: str) -> bool:
    with own_write_reads():
        row = await get_client().query_one(
            f"""
            SELECT 1 AS x FROM {TABLE} FINAL
            WHERE active = 0 AND id != {{id:String}} AND has(subjects, {{s:String}})
            LIMIT 1
            """,
            params={"id": uid, "s": subject},
        )
    return row is not None


async def _write(row: dict[str, Any]) -> None:
    ch = get_client()
    version = await next_version(ch, TABLE, "id", row["id"])
    row.update(updated_at=datetime.now(UTC), version=version)
    await ch.insert(TABLE, [{c: row[c] for c in COLUMNS}])


def _keyed(subject: str) -> str:
    """A hash of `subject` keyed with the root secret: a list of addresses
    does not reverse it, as it would an unkeyed one."""
    key = hmac.new(str(settings.jwt_secret).encode(), b"scim-audit", hashlib.sha256).digest()
    return hmac.new(key, subject.encode(), hashlib.sha256).hexdigest()


async def _audit(event: str, row: dict[str, Any]) -> None:
    # No address or subject in clear: the SCIM id (which the provider holds
    # with the email) and keyed hashes of the subjects.
    await audit.emit(
        event,
        org="-",
        namespace="-",
        actor=_BY,
        subject={"id": row["id"], "subjects": [_keyed(s) for s in row["subjects"]]},
    )
