"""Who an identity provider has deactivated over SCIM 2.0 (`loom.scim_users`).

A SCIM user is an email the provider manages; Loom makes no account for one.
Deactivating revokes outright every subject an SSO sign-in with the email could
reach and refuses that sign-in; reactivating lifts both and leaves the tokens
issued before it dead (docs/operations/sso.md).

SCIM's revocations are its own, under `revocation.SCIM_PREFIX`: it never
writes the row an operator's `loom revoke-token` writes, and a reactivation
lifts only SCIM's. A subject another deactivated user still reaches stays
revoked.

Leases are always taken in this order: the user's, then a userName's it takes
(create, rename), then one per subject the user reaches, in sorted order.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from server.auth import login, revocation, service_accounts
from server.core import leases
from server.core.ch import get_client, own_write_reads
from server.core.datetimes import as_aware_utc
from server.core.grant_sql import LIVE_GRANT
from server.core.versions import next_version
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)

TABLE = "loom.scim_users"
COLUMNS = (
    "id",
    "user_name",
    "email",
    "emails",
    "addresses",
    "work_addresses",
    "active",
    "deleted",
    "subjects",
    "created_at",
    "updated_at",
    "version",
)
# A PATCH as a function of the row it applies to: the userName and `emails` it leaves.
Patch = Callable[[dict[str, Any]], tuple[str | None, list[dict[str, Any]] | None]]
# The reasons on the revocations a deactivation and a reactivation write.
DEACTIVATED = "scim: deactivated"
REACTIVATED = "scim: reactivated"
_BY = "scim"
# SCIM ids are uuid5 of the email: the same across replicas, retries and a
# root-secret rotation (docs/architecture/security.md says why it is unkeyed).
_ID_NAMESPACE = uuid.UUID("6f1c3e0a-5b7d-4c2e-9a41-2d8f0b6e7c13")
_LEASE_TTL_S = 60.0
_LEASE_WAIT_S = 10.0


class Exists(Exception):
    """A live SCIM user already has this email or userName."""


class Busy(Exception):
    """Another request is changing this user; retry shortly."""


def tokens(raw: str) -> list[str]:
    """The SCIM bearer tokens a `LOOM_SCIM_TOKEN` value lists; none means SCIM is off."""
    return [t.strip() for t in raw.split(",") if t.strip()]


def normalized(address: str) -> str:
    return address.strip().lower()


def user_id(email: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, normalized(email)))


async def get(uid: str) -> dict[str, Any] | None:
    """The SCIM user `uid`, unless absent or deleted."""
    row = await _row(uid)
    return None if row is None or row["deleted"] else row


async def find(user_name: str) -> dict[str, Any] | None:
    """The live SCIM user whose userName is `user_name`, in any case (RFC 7643 §4.1.1)."""
    row = await get_client().query_one(
        f"SELECT * FROM {TABLE} FINAL "
        "WHERE lowerUTF8(user_name) = lowerUTF8({n:String}) AND deleted = 0 LIMIT 1",
        params={"n": user_name.strip()},
    )
    return None if row is None else dict(row)


async def page(
    start: int, count: int, *, email: str | None = None, work: bool = False
) -> tuple[int, list[dict[str, Any]]]:
    """The total of live SCIM users, with `email` among their `emails` entries
    when given (their work entries when `work`), and those from `start`
    (1-based), at most `count`."""
    ch = get_client()
    where = "deleted = 0"
    params: dict[str, Any] = {"count": count, "skip": max(0, start - 1)}
    if email is not None:
        where += f" AND has({'work_addresses' if work else 'addresses'}, {{a:String}})"
        params["a"] = normalized(email)
    total = await ch.query_one(
        f"SELECT count() AS n FROM {TABLE} FINAL WHERE {where}", params=params
    )
    rows = await ch.query_all(
        f"""
        SELECT * FROM {TABLE} FINAL WHERE {where}
        ORDER BY user_name LIMIT {{count:UInt32}} OFFSET {{skip:UInt32}}
        """,
        params=params,
    )
    return int((total or {}).get("n") or 0), [dict(r) for r in rows]


async def deactivated(email: str) -> bool:
    """Whether SCIM has deactivated `email`, deleted users included, also once
    SCIM is turned off: a deactivated email with no account has no revocation,
    only this row, to keep it out. Raises `SignInUnavailable` when it cannot
    read: a sign-in must not pass unchecked."""
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
    user_name: str, email: str, *, active: bool, emails: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record the SCIM user for `email`. Raises Exists when one is live,
    once a retried re-create has waited out its cutoff. A deleted one comes
    back, still deactivated unless `active`. `emails` is the provider's list
    the email was keyed from."""
    uid = user_id(email)
    async with _held(_user_lease(uid)), _held(_name_lease(user_name)):
        row = await _row(uid)
        exists = row is not None and not row["deleted"]
        if row is None:
            row = {
                "id": uid,
                "email": normalized(email),
                "emails": "[]",
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
            _note_another_name(uid, user_name, email)
            changes = {"user_name": user_name.strip(), "deleted": 0, "emails": json.dumps(emails)}
            row, cutoff = await _set_active(row, active, **changes)
    if not row["active"]:  # a retry too: it finishes what a failed try left
        await _revoke_tokens(row)
    await _wait_out(cutoff)
    if exists:
        raise Exists
    return row


async def update(
    uid: str,
    *,
    active: bool | None = None,
    user_name: str | None = None,
    emails: list[dict[str, Any]] | None = None,
    patched: Patch | None = None,
) -> dict[str, Any] | None:
    """Set `active`, rename the userName (it is not the key) and/or replace
    the provider's `emails` list; None when there is no such user. `patched`
    computes the userName and list from the row read under the user's lease,
    so two PATCHes at once each apply to what the other left."""
    async with _held(_user_lease(uid)):
        row = await get(uid)
        if row is None:
            return None
        if patched is not None:
            user_name, emails = patched(row)
        changes = {} if emails is None else {"emails": json.dumps(emails)}
        if not user_name or user_name.strip() == row["user_name"]:
            new, cutoff = await _set_active(row, active, **changes)
        else:
            async with _held(_name_lease(user_name)):
                await _refuse_taken(user_name, uid)
                renamed = user_name.strip()  # kept as sent, matched in any case
                _note_another_name(uid, renamed, row["email"])
                new, cutoff = await _set_active(row, active, user_name=renamed, **changes)
    if not new["active"]:  # a retry too: it finishes what a failed try left
        await _revoke_tokens(new)
    await _wait_out(cutoff)
    return new


def _note_another_name(uid: str, user_name: str, email: str) -> None:
    if normalized(user_name) != normalized(email):
        # Deactivations follow the email: say so where an operator will look.
        log.warning(
            "scim: user %s has a userName other than its email; Loom keys on the email", uid
        )


def emails_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The provider's `emails` list as last written: value, type, primary."""
    return list(json.loads(row["emails"]))


def is_primary(value: Any) -> bool:
    """A SCIM `primary` flag as providers send it (true, "True")."""
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
        # A retry finishes a failed try. The addresses beside the email keep no one out.
        row, _ = await _set_active(row, False, deleted=1, emails="[]")
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


def _subject_lease(subject: str) -> str:
    # Hashed, but not keyed as the audit's: the CLI may not hold the root secret.
    return f"scim-subject:{uuid.uuid5(_ID_NAMESPACE, subject)}"


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
async def subject_leases(subjects: list[str]) -> AsyncIterator[None]:
    """One lease per subject, in sorted order, so users sharing one never
    interleave their revocations and two never wait on each other in a cycle.
    An operator's `loom revoke-token --scim` takes it too: it never lands
    between a reactivation's read of a revocation and its lift."""
    async with AsyncExitStack() as held:
        for subject in sorted(set(subjects)):
            await held.enter_async_context(_held(_subject_lease(subject)))
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
    """The row as written, and the cutoff of what it lifted for the caller to
    wait out once it lets its leases go (one long past costs nothing). `active`
    None keeps the user as they are: an active one converges, an inactive one
    repeats its deactivation."""
    if active or (active is None and row["active"]):
        return await _converge_active(row, bool(active), **changes)
    return await _deactivate(row, **changes), None


async def _deactivate(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    while True:
        new = {**row, **changes, "active": 0}
        found = await _subjects(row["email"])
        known = set(row["subjects"])
        async with subject_leases([*known, *found]):
            # Going inactive revokes, and records, what the email reaches now: an
            # account an operator has since moved to another email is that person's.
            if row["active"]:
                new["subjects"] = subjects = sorted(found)
            else:
                # A repeat also re-revokes what a reactivation let go while out of
                # reach; an operator's give-back of a subject this user knew stays.
                new["subjects"] = sorted(known | set(found))
                rows = await revocation.scim_revocations(sorted(found))
                given_back = {s for s in known if s in rows and rows[s]["revoked_by"] != _BY}
                subjects = sorted(set(found) - _outright(rows) - given_back)
            for subject in subjects:
                await revocation.scim_revoke(
                    subject, reason=DEACTIVATED, revoked_by=_BY, reread=False
                )
                await _flag_collateral(new["id"], subject, found.get(subject, set()))
            await _write_audited(row, new)
        # The snapshot re-read each revocation would make, once and past the leases.
        await revocation.refresh()
        if not set(await _subjects(row["email"])) - set(new["subjects"]):
            return new
        # An account made before the row said inactive (a first sign-in's JIT):
        # revoke it too, with the sessions and tokens it made meanwhile.
        row, changes = new, {}


async def _converge_active(
    row: dict[str, Any], reactivating: bool, **changes: Any
) -> tuple[dict[str, Any], datetime | None]:
    """The only way a user ends active: write the row, then lift what SCIM still
    revokes outright of every subject the email reaches, whatever a failed
    request left. The cutoff is for the caller to wait out: what this call
    lifted, and for a reactivation (or its retry) the newest SCIM narrowed to."""
    new = {**row, **changes, "active": 1}
    subjects = sorted(set(row["subjects"]) | set(await _subjects(row["email"])))
    rows = await revocation.scim_revocations(subjects)
    if not _outright(rows):  # nothing to lift, as on most pushes: no subject lease
        await _write_audited(row, new)
        return new, _newest_cutoff(rows) if reactivating else None
    async with subject_leases(subjects):
        # Taken before the row: a token a sign-in gets once the row says active
        # is past it from the next second on. And past every token issued before
        # the revocations it lifts, by a replica whose clock runs ahead.
        lift_to = max(
            datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1),
            *(
                revocation.past_every_token_before(as_aware_utc(r["revoked_at"]))
                for r in rows.values()
                if r["issued_before"] is None
            ),
        )
        # The row first: a lift that fails leaves the user active and still
        # revoked, never inactive with a subject let go. A retry lifts the rest.
        await _write_audited(row, new)
        lifted = await _lift(row, subjects, lift_to)
    await revocation.refresh()
    if lifted:
        return new, lift_to
    return new, _newest_cutoff(rows) if reactivating else None


async def _write_audited(row: dict[str, Any], new: dict[str, Any]) -> None:
    """Write the row, then audit its transition. A write that raised may have
    landed and lost its reply, and a retry would find the row changed and
    audit nothing: so the row is read back, and what landed is audited."""
    from server.auth.authz import forget_ended

    try:
        if new != row or "version" not in row:  # a change, or a user not yet written
            await _write(new)
    except Exception:  # re-raised: what landed is audited before the error goes on
        try:
            landed = await _row(row["id"])
        except Exception:  # noqa: BLE001 — the write's own error is the one to raise
            landed = None
        if landed is not None:
            await _audit_transition(row, landed)
        raise
    finally:  # this process's acted-for check reads the row afresh
        forget_ended("scim", row["email"])  # new["email"] is the same: the id derives from it
    await _audit_transition(row, new)


async def _audit_transition(row: dict[str, Any], new: dict[str, Any]) -> None:
    if bool(new["active"]) != bool(row["active"]):
        await _audit("auth.scim_reactivated" if new["active"] else "auth.scim_deactivated", new)
    if new["deleted"] and not row["deleted"]:
        await _audit("auth.scim_deleted", new)


async def _subjects(email: str) -> dict[str, set[str]]:
    """Every subject an SSO sign-in with `email` could reach, in any org, and
    the orgs it is reached in. Unlike sign-in, several are no reason to stop:
    each is revoked."""
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
    found: dict[str, set[str]] = {}
    for r in rows:
        found.setdefault(r["owner_email"], set()).add(r["org"])
    return found


async def _flag_collateral(uid: str, subject: str, orgs: set[str]) -> None:
    """Log and audit `subject`, just revoked, when it also holds accounts or
    grants beyond `orgs`, the orgs where the email reaches that subject: a
    subject is a name, not an org's, and the same one may be someone else's
    elsewhere."""
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


async def _lift(row: dict[str, Any], subjects: list[str], cutoff: datetime) -> bool:
    """Narrow SCIM's outright revocations of `subjects` to the tokens issued
    before `cutoff`; whether it narrowed any. A subject another deactivated (or
    deleted) user also reaches stays revoked: lifting it would let that one back in."""
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
    return lifted


def _outright(rows: dict[str, dict[str, Any]]) -> set[str]:
    """The subjects SCIM revokes outright (an operator's `--scim` row always narrows)."""
    return {s for s, r in rows.items() if r["issued_before"] is None}


def _newest_cutoff(rows: dict[str, dict[str, Any]]) -> datetime | None:
    """The newest cutoff a reactivation narrowed SCIM's revocations to; an
    operator's (`--scim`) is never waited out."""
    cutoffs: list[datetime] = [
        r["issued_before"] for r in rows.values() if r["issued_before"] and r["revoked_by"] == _BY
    ]
    if not cutoffs:
        return None
    return as_aware_utc(max(cutoffs))


async def _held_by_another_inactive(uid: str, subject: str) -> bool:
    """Whether another deactivated (or deleted) user's email reaches `subject`
    now, by `_subjects`' rules."""
    with own_write_reads():
        row = await get_client().query_one(
            f"""
            SELECT 1 AS x FROM {TABLE} FINAL
            WHERE active = 0 AND id != {{id:String}} AND email IN (
                SELECT contact_email FROM loom.namespaces FINAL
                WHERE owner_email = {{s:String}} AND active = 1
                  AND (password_hash = '' OR (org, namespace, contact_email) IN (
                      SELECT org, namespace, email FROM loom.sso_links FINAL
                      WHERE linked = 1
                  ))
            )
            LIMIT 1
            """,
            params={"id": uid, "s": subject},
        )
    return row is not None


async def _write(row: dict[str, Any]) -> None:
    ch = get_client()
    version = await next_version(ch, TABLE, {"id": row["id"]})
    entries = [e for e in emails_of(row) if normalized(e["value"])]
    row.update(
        addresses=[normalized(e["value"]) for e in entries],
        work_addresses=[normalized(e["value"]) for e in entries if normalized(e["type"]) == "work"],
        updated_at=datetime.now(UTC),
        version=version,
    )
    await ch.insert(TABLE, [{c: row[c] for c in COLUMNS}])


def _keyed(subject: str) -> str:
    """A hash of `subject` keyed with the root secret: a list of addresses
    does not reverse it, as it would an unkeyed one."""
    key = hmac.new(settings.jwt_secret.encode(), b"scim-audit", hashlib.sha256).digest()
    return hmac.new(key, subject.encode(), hashlib.sha256).hexdigest()


async def _audit(event: str, row: dict[str, Any]) -> None:
    # No address or subject in clear: the SCIM id (a pseudonym the provider
    # holds with the email) and keyed hashes of the subjects.
    await audit.emit(
        event,
        org="-",
        namespace="-",
        actor=_BY,
        subject={"id": row["id"], "subjects": [_keyed(s) for s in row["subjects"]]},
    )
