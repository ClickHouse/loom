"""Server-side idempotency cache backed by `loom.idempotency`.

Best-effort, not cluster-consistent: two requests with the same
Idempotency-Key racing on different replicas both miss and execute the
handler (ReplacingMergeTree dedupes the stored rows, so later replays
are correct). That's fine for the idempotent ops that use this cache —
the second write overwrites with identical content. Ops declared
`idempotent=False` bypass it.

A stored answer belongs to whoever caused it: the row key
is `scoped_key` over the caller's org, subject, the operation and the
client's key, the row carries that org and subject for erasure, and both
transports consult the cache only after the token, authorization and
policy gates have admitted the call. A replay therefore
never reaches an anonymous caller, another tenant or another operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now

log = logging.getLogger(__name__)

TTL = timedelta(hours=24)


@dataclass(frozen=True)
class CacheHit:
    status_code: int
    body: dict[str, Any] | list[Any] | str | int | float | bool | None
    headers: dict[str, str]


@dataclass(frozen=True)
class CacheResult:
    """Result of `check()`. At most one of `hit` / `conflict` is set;
    a fresh miss is `(hit=None, conflict=False)`."""

    hit: CacheHit | None
    conflict: bool


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
    """Lookup the cache. Returns (hit, conflict) tuple semantics."""
    try:
        row = await get_client().query_one(
            "SELECT body_hash, status_code, response_body, response_headers, expires_at "
            "FROM loom.idempotency FINAL WHERE idempotency_key = {k:String}",
            params={"k": idempotency_key},
        )
    except Exception:  # the cache fails open: a failed read is a miss
        log.exception("idempotency.check failed; treating as miss")
        return CacheResult(hit=None, conflict=False)

    if row is None:
        return CacheResult(hit=None, conflict=False)

    # CH returns naive datetimes; we normalize to naive on write.
    now_naive = naive_utc_now()
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime) and expires_at < now_naive:
        return CacheResult(hit=None, conflict=False)

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
