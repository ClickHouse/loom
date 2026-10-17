"""Who an identity provider has deactivated over SCIM 2.0 (`loom.scim_users`).

A SCIM user is an email the provider manages (its `userName`). Loom makes no
account for one: JIT makes that at the person's first sign-in, as without
SCIM. What SCIM changes is `active`:

- Deactivating revokes the subject the email signs in to (`resolve_identity_for_email`)
  outright: every session and every token the person made, since a delegated
  token names them as its creator or a parent. SSO sign-in with the email is
  refused while it lasts, even when it reaches no account yet.
- Reactivating lifts the refusal and narrows that revocation to the tokens
  issued before now, so the person signs in again and the old tokens stay dead.

The account and its data stay: this is not erasure. A subject an operator
revoked outright stays revoked through both. Every transition runs under a
lease on the user, and each step is idempotent, so a provider's retry of a
half-done one finishes it.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from server.observability import audit

log = logging.getLogger(__name__)

TABLE = "loom.scim_users"
# The reason on the revocation a deactivation writes: a reactivation narrows
# only a revocation with this reason, never an operator's.
DEACTIVATED = "scim: deactivated"
_BY = "scim"
# SCIM ids are uuid5 of the email under this namespace: one id per person,
# whichever replica or retry computes it.
_ID_NAMESPACE = uuid.UUID("6f1c3e0a-5b7d-4c2e-9a41-2d8f0b6e7c13")
_LEASE_TTL_S = 60.0
_LEASE_WAIT_S = 10.0


class Exists(Exception):
    """A live SCIM user already has this userName."""


class Busy(Exception):
    """Another request is changing this user; retry shortly."""


def normalized(user_name: str) -> str:
    return user_name.strip().lower()


def user_id(user_name: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, normalized(user_name)))


async def get(uid: str) -> dict[str, Any] | None:
    """The SCIM user `uid`, unless absent or deleted."""
    row = await _row(uid)
    return None if row is None or row["deleted"] else row


async def find(user_name: str) -> dict[str, Any] | None:
    return await get(user_id(user_name))


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
    """Whether SCIM has deactivated `email`, deleted users included. Raises
    `SignInUnavailable` when it cannot read: a sign-in must not pass unchecked."""
    try:
        with own_write_reads():  # a deactivation may have landed a moment ago elsewhere
            row = await get_client().query_one(
                f"SELECT 1 AS x FROM {TABLE} FINAL WHERE id = {{id:String}} AND active = 0",
                params={"id": user_id(email)},
            )
    except Exception as e:  # a read that failed or ran out of time: retryable
        raise login.SignInUnavailable from e
    return row is not None


async def create(user_name: str, *, active: bool) -> dict[str, Any]:
    """Record the SCIM user `user_name` (an email). Raises Exists when one is
    live. A deleted one comes back, still deactivated unless `active`."""
    uid = user_id(user_name)
    async with _held(uid):
        row = await _row(uid)
        if row is not None and not row["deleted"]:
            raise Exists
        if row is None:
            row = {
                "id": uid,
                "user_name": normalized(user_name),
                "active": 1,
                "deleted": 0,
                "subject": "",
                "org": "",
                "created_at": datetime.now(UTC),
            }
        return await _set_active(row, active, deleted=0)


async def set_active(uid: str, active: bool) -> dict[str, Any] | None:
    """Deactivate or reactivate `uid`; None when there is no such user."""
    async with _held(uid):
        row = await get(uid)
        return None if row is None else await _set_active(row, active)


async def delete(uid: str) -> bool:
    """A SCIM DELETE: the user is deactivated and answers 404 from now on."""
    async with _held(uid):
        row = await get(uid)
        if row is None:
            return False
        await _set_active(row, False, deleted=1)
        return True


@asynccontextmanager
async def _held(uid: str) -> AsyncIterator[None]:
    async with leases.holding(f"scim-user:{uid}", ttl_s=_LEASE_TTL_S, wait_s=_LEASE_WAIT_S) as held:
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
    if active and not was_active:
        await _lift(row["subject"])
    elif not active:
        if was_active or not row["subject"]:
            new["subject"], new["org"] = await _account(row["user_name"])
        await _revoke(new["subject"])  # again on a retry: the last try may have stopped short
    if new != row or "version" not in row:  # a change, or a user not yet written
        await _write(new)
    if active != was_active:
        await _audit(new)
    return new


async def _account(email: str) -> tuple[str, str]:
    """The (subject, org) an SSO sign-in with `email` reaches, or ('', '')."""
    with own_write_reads():
        identity = await login.resolve_identity_for_email(email)
    if identity is None:
        # No account yet, or several that disagree: sign-in is refused by email either way.
        log.info("scim: %s reaches no single account; refusing its sign-in only", user_id(email))
        return "", ""
    subject, org, _, _ = identity
    return subject, org


async def _revoke(subject: str) -> None:
    """Revoke `subject` outright unless it already is (by an operator, or by
    the try this one retries)."""
    if subject and not await revocation.revoked_outright(subject):
        await revocation.revoke(subject, reason=DEACTIVATED, revoked_by=_BY)


async def _lift(subject: str) -> None:
    """Narrow the deactivation's revocation to the tokens issued so far, and wait
    out this second: a token issued in it would fall before the cutoff."""
    if not subject:
        return
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    if await revocation.narrow(subject, reason=DEACTIVATED, issued_before=cutoff, revoked_by=_BY):
        await asyncio.sleep(max(0.0, cutoff.timestamp() - time.time()))


async def _write(row: dict[str, Any]) -> None:
    ch = get_client()
    # Past the user's newest row, whichever replica's clock wrote it.
    with own_write_reads():
        newest = await ch.query_one(
            f"SELECT max(version) AS v FROM {TABLE} WHERE id = {{id:String}}",
            params={"id": row["id"]},
        )
    version = max(time.time_ns(), int((newest or {}).get("v") or 0) + 1)
    row.update(updated_at=datetime.now(UTC), version=version)
    await ch.insert(TABLE, [{c: row[c] for c in COLUMNS}])


COLUMNS = (
    "id",
    "user_name",
    "active",
    "deleted",
    "subject",
    "org",
    "created_at",
    "updated_at",
    "version",
)


async def _audit(row: dict[str, Any]) -> None:
    # Hashes only: the audit log is no place for the address or the subject.
    subject_hash = hashlib.sha256(row["subject"].encode()).hexdigest() if row["subject"] else ""
    await audit.emit(
        "auth.scim_reactivated" if row["active"] else "auth.scim_deactivated",
        org=row["org"] or "-",
        namespace="-",
        actor=_BY,
        subject={"id": row["id"], "subject_sha256": subject_hash},
    )
