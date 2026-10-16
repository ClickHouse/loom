"""Server-side idempotency cache backed by `loom.idempotency`.

The lookup reads with sequential consistency, so a retry landing on another
replica sees an answer stored a moment ago. A miss takes the key's deployment-wide
in-progress claim (`server.core.leases`) until the request has answered, by which
time the answer is stored: a duplicate arriving meanwhile, on any replica, is
answered IDEMPOTENT_IN_PROGRESS (retry after a moment) instead of running the
handler twice. The cache fails open — a failed read, or a claim store
that cannot answer, is a plain miss. Ops declared `idempotent=False` and read-only
ops bypass it.

A stored answer belongs to whoever caused it: the row key
is `scoped_key` over the caller's org, subject, the operation and the
client's key, the row carries that org and subject for erasure, and both
transports consult the cache only after the token, authorization and
policy gates have admitted the call. A replay therefore
never reaches an anonymous caller, another tenant or another operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from server.core import leases
from server.core.background import TaskSet
from server.core.ch import get_client, own_write_reads
from server.core.datetimes import naive_utc_now

log = logging.getLogger(__name__)

TTL = timedelta(hours=24)

# The scoped key of the request being served ("" without one). The memory journal
# keeps it on each commit, past this cache's TTL, so a retry days later is still
# recognised and a reused key with another body is still refused.
request_key_var: ContextVar[str] = ContextVar("loom_idempotency_key", default="")
# The body hash that key was sent with: the journal compares it on a retry.
request_body_var: ContextVar[str] = ContextVar("loom_idempotency_body", default="")


@dataclass(frozen=True)
class CacheHit:
    status_code: int
    body: dict[str, Any] | list[Any] | str | int | float | bool | None
    headers: dict[str, str]


@dataclass(frozen=True)
class CacheResult:
    """Result of `check()`. At most one of `hit` / `conflict` is set;
    a fresh miss is `(hit=None, conflict=False)`. `in_progress` (with
    `conflict`): a request with this key is still running."""

    hit: CacheHit | None
    conflict: bool
    in_progress: bool = False


# The in-progress claim is renewed while the handler runs; a holder that dies
# frees the key after this.
IN_PROGRESS_TTL_S = 60
_releases = TaskSet()


def hash_body(body: Any) -> str:
    """Deterministic content hash. Sorted keys; tolerates `None` body."""
    return hashlib.sha256(
        json.dumps(body if body is not None else {}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def request_hash(payload: Any, *, unset: tuple[str, ...] = ()) -> str:
    """The hash an idempotency row keys a request's body on: the op's
    validated input, every field as the handler receives it. Both transports
    hash it, so a key sent over HTTP replays over MCP and back. `unset` names
    fields a transport filled on the caller's behalf (MCP's session run) that
    reset to the model default first — they say which connection carried
    the call, not what was asked."""
    body = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    if unset and isinstance(body, dict):
        fields = type(payload).model_fields
        for name in unset:
            if name in fields:
                body[name] = fields[name].get_default(call_default_factory=True)
    return hash_body(body)


def scoped_key(client_key: str, *, org: str, subject: str, operation: str) -> str:
    """The stored row key for `client_key` sent by (org, subject) to
    `operation`, hashed so the ORDER BY column keeps one fixed width.

    The parts are hashed as a JSON array, never joined on a separator: a
    separator a caller can put in its own key or subject would let two
    different tuples encode to the same bytes. "v3" marks rows keyed this way."""
    parts = ["v3", org, subject, operation, client_key]
    encoded = json.dumps(parts, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Scope:
    """One request's replay row: its key, and the owner the row is stamped
    with. The key is a hash, so erasure finds an org's or a principal's
    cached answers by `org` and `subject`."""

    key: str
    org: str
    subject: str


def scope(client_key: str, *, org: str, subject: str, operation: str) -> Scope:
    key = scoped_key(client_key, org=org, subject=subject, operation=operation)
    return Scope(key=key, org=org, subject=subject)


def _to_str(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return str(v)


async def check(idempotency_key: str, body_hash: str) -> CacheResult:
    """Lookup the cache. Returns (hit, conflict) tuple semantics.

    Inside a request, the key's in-progress claim is taken while the stored answer
    is read, and held until the request has answered. Held elsewhere: the other
    request may have stored its answer since, so the row is read once more before
    answering IDEMPOTENT_IN_PROGRESS."""
    claiming = asyncio.ensure_future(_claim(idempotency_key))
    try:
        with own_write_reads():  # an answer another replica stored a moment ago
            row = await _stored(idempotency_key)
    except asyncio.CancelledError:
        _drop(claiming)
        raise
    except Exception:  # the cache fails open: a failed read is a miss
        log.exception("idempotency.check failed; treating as miss")
        row = None
    found = _result(row, body_hash) if row is not None else None
    free, claim = await claiming
    if found is not None:
        if claim is not None:
            await _release_beside_answer(claim)  # a replay runs no handler
        return found
    if claim is not None:
        from server.core import request_tail

        request_tail.defer(partial(_release_beside_answer, claim))
    if free:
        return CacheResult(hit=None, conflict=False)
    try:
        with own_write_reads():
            row = await _stored(idempotency_key)
    except Exception:  # noqa: BLE001 — the other request still runs: refused below
        row = None
    found = _result(row, body_hash) if row is not None else None
    return found or CacheResult(hit=None, conflict=True, in_progress=True)


async def _stored(idempotency_key: str) -> dict[str, Any] | None:
    return await get_client().query_one(
        "SELECT body_hash, status_code, response_body, response_headers, expires_at "
        "FROM loom.idempotency FINAL WHERE idempotency_key = {k:String}",
        params={"k": idempotency_key},
    )


async def _claim(idempotency_key: str) -> tuple[bool, leases.Lease | None]:
    """(free, claim): whether no other request holds the key, and the claim taken
    for this one. Only inside a request; a claim store that cannot answer is free."""
    from server.core import request_tail

    if not request_tail.active():
        return True, None
    try:
        # The client's key is a fresh UUID: inserted without reading it first.
        claim = await leases.take(
            f"idem:{idempotency_key}", ttl_s=IN_PROGRESS_TTL_S, renew=True, expect_free=True
        )
    except Exception:  # noqa: BLE001 — the cache fails open: no claim is a plain miss
        log.warning("idempotency: in-progress claim unavailable; treating as miss")
        return True, None
    return claim is not None, claim


def _drop(claiming: asyncio.Future[tuple[bool, leases.Lease | None]]) -> None:
    """A check cancelled during its read gives its claim up (a cancelled take releases
    what it may have written)."""
    if not claiming.done():
        claiming.cancel()
    elif not claiming.cancelled() and (claim := claiming.result()[1]) is not None:
        _releases.spawn(claim.release(), name="loom-idempotency-release")


async def _release_beside_answer(claim: leases.Lease) -> None:
    """The answer is stored when the tail runs, and a duplicate reads it before the
    claim: the release need not hold the answer back."""
    _releases.spawn(claim.release(), name="loom-idempotency-release")


def _result(row: dict[str, Any], body_hash: str) -> CacheResult | None:
    """The stored row's verdict; None when it has expired (a miss)."""
    # CH returns naive datetimes; we normalize to naive on write.
    now_naive = naive_utc_now()
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime) and expires_at < now_naive:
        return None

    stored_body_hash = _to_str(row["body_hash"])
    if stored_body_hash != body_hash:
        return CacheResult(hit=None, conflict=True)

    return CacheResult(
        hit=CacheHit(
            status_code=int(row["status_code"]),
            body=json.loads(row["response_body"]) if row["response_body"] else None,
            headers=json.loads(row["response_headers"] or "{}"),
        ),
        conflict=False,
    )


async def store(
    idempotency_key: str,
    body_hash: str,
    operation: str,
    status_code: int,
    response_body: Any,
    response_headers: dict[str, str] | None = None,
    *,
    org: str,
    subject: str,
) -> None:
    """Best-effort cache write after a successful handler. Never raises:
    a failed insert just means the next retry re-executes the handler.
    `org` and `subject` are the admitted caller's (the `Scope` the key was
    made from), so erasure can delete the row."""
    now = naive_utc_now()
    try:
        await get_client().insert(
            "loom.idempotency",
            [
                {
                    "idempotency_key": idempotency_key,
                    "org": org,
                    "subject": subject,
                    "body_hash": body_hash,
                    "operation": operation,
                    "status_code": status_code,
                    "response_body": json.dumps(response_body, sort_keys=True, default=str),
                    "response_headers": json.dumps(response_headers or {}, sort_keys=True),
                    "created_at": now,
                    "expires_at": now + TTL,
                }
            ],
        )
    except Exception:  # the cache fails open: a failed write re-executes on retry
        log.exception("idempotency.store failed for key=%s op=%s", idempotency_key, operation)
