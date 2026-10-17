"""Who an identity provider has deactivated over SCIM 2.0 (`loom.scim_users`).

A SCIM user is an email the provider manages. Loom makes no account for one:
JIT makes that at the person's first sign-in, as without SCIM. What SCIM
changes is `active`:

- Deactivating revokes outright every subject an SSO sign-in with the email
  could reach (an SSO account naming it, or a password account linked to it),
  in every org: every session and every token those subjects made, since a
  delegated token names them as its creator or a parent. SSO sign-in with the
  email is refused while it lasts, even when it reaches no account yet.
- Reactivating lifts the refusal and narrows those revocations to the tokens
  issued before now, so the person signs in again and the old tokens stay dead.

The accounts and their data stay: this is not erasure. A subject an operator
revoked outright stays revoked through both, and so does one another
deactivated user still reaches. Leases, always taken in this order: the
user's, then a userName's it takes (create, rename), then `scim-subjects`
around the revocations and the row they belong with, so two users sharing a
subject never interleave. Each step is idempotent, so a provider's retry of a
half-done one finishes it. With `LOOM_SCIM_TOKEN` empty nothing here is read.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from server.auth import login, revocation
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
# The reason on the revocation a deactivation writes: a reactivation narrows
# only a revocation with this reason, never an operator's.
DEACTIVATED = "scim: deactivated"
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
    """Record the SCIM user for `email`. Raises Exists when one is live. A
    deleted one comes back, still deactivated unless `active`. `email_entry`
    is the type and primary flag of the entry the email came from."""
    uid = user_id(email)
    async with _held(_user_lease(uid)), _held(_name_lease(user_name)):
        row = await _row(uid)
        if row is not None and not row["deleted"]:
            raise Exists
        await _refuse_taken(user_name, uid)
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
        changes = {"user_name": normalized(user_name), "deleted": 0, **_entry(email_entry)}
        return await _set_active(row, active, **changes)


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
        keep = bool(row["active"]) if active is None else active
        changes = _entry(email_entry)
        if not user_name or normalized(user_name) == row["user_name"]:
            return await _set_active(row, keep, **changes)
        async with _held(_name_lease(user_name)):
            await _refuse_taken(user_name, uid)
            return await _set_active(row, keep, user_name=normalized(user_name), **changes)


def _entry(email_entry: dict[str, Any] | None) -> dict[str, Any]:
    if email_entry is None:
        return {}
    return {
        "email_type": str(email_entry.get("type") or "").lower(),
        "email_primary": int(email_entry.get("primary") in (True, "true")),
    }


async def delete(uid: str) -> bool:
    """A SCIM DELETE: the user is deactivated and answers 404 from now on."""
    async with _held(_user_lease(uid)):
        row = await get(uid)
        if row is None:
            return False
        row = await _set_active(row, False, deleted=1)
        await _audit("auth.scim_deleted", row)
        return True


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


_SUBJECTS_LEASE = "scim-subjects"


@asynccontextmanager
async def _held(name: str) -> AsyncIterator[None]:
    async with leases.holding(name, ttl_s=_LEASE_TTL_S, wait_s=_LEASE_WAIT_S) as held:
        if not held:
            raise Busy
        yield


async def _row(uid: str) -> dict[str, Any] | None:
    with own_write_reads():
        row = await get_client().query_one(
            f"SELECT * FROM {TABLE} FINAL WHERE id = {{id:String}}", params={"id": uid}
        )
    return None if row is None else dict(row)


async def _set_active(row: dict[str, Any], active: bool, **changes: Any) -> dict[str, Any]:
    was_active = bool(row["active"])
    new = {**row, **changes, "active": int(active)}
    cutoff = None
    async with _held(_SUBJECTS_LEASE):
        if active and not was_active:
            cutoff = await _lift(row["id"], row["subjects"])
        elif not active:
            found = await _subjects(row["email"])
            known = set(row["subjects"])
            new["subjects"] = sorted(known | set(found))
            # Going inactive (or retrying a try that stopped short of writing the
            # row) revokes them all; once inactive, only accounts found since, so
            # one an operator narrowed stays narrowed.
            first = was_active or "version" not in row
            for subject in new["subjects"] if first else sorted(set(found) - known):
                await _revoke(new["id"], subject, set(found.values()))
        if new != row or "version" not in row:  # a change, or a user not yet written
            await _write(new)
    if cutoff is not None:  # wait out the cutoff's second, a token issued in it falls before it
        await asyncio.sleep(max(0.0, cutoff.timestamp() - time.time()))
    if active != was_active:
        await _audit("auth.scim_reactivated" if active else "auth.scim_deactivated", new)
    return new


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


async def _revoke(uid: str, subject: str, orgs: set[str]) -> None:
    """Revoke `subject` outright unless it already is (by an operator, or by
    the try this one retries). A subject is a name, not an org's: the same one
    may be someone else's elsewhere, and the revocation reaches them too."""
    # The store, not this process's snapshot: one behind would skip a revocation needed again.
    if await revocation.has_outright_row(subject):
        return
    elsewhere = await _orgs_beyond(subject, orgs)
    if elsewhere:
        # Failing closed, it is still revoked; an operator sees it and can narrow it.
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
    await revocation.revoke(subject, reason=DEACTIVATED, revoked_by=_BY)


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


async def _lift(uid: str, subjects: list[str]) -> datetime | None:
    """Narrow the deactivation's revocations to the tokens issued so far; the
    cutoff when one was narrowed, for the caller to wait out. A subject another
    deactivated (or deleted) user also reaches stays revoked: lifting it would
    let that one back in."""
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    lifted = False
    for subject in subjects:
        if await _held_by_another_inactive(uid, subject):
            log.warning(
                "scim: reactivating %s leaves a subject revoked that another deactivated "
                "user still reaches",
                uid,
            )
            continue
        lifted |= await revocation.narrow(
            subject, reason=DEACTIVATED, issued_before=cutoff, revoked_by=_BY
        )
    return cutoff if lifted else None


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
