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

from server.core import leases, pg
from server.core.background import TaskSet
from server.core.ch import get_client, own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.error_summary import error_summary
from server.core.errors import NotFoundError

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
    a fresh miss is `(hit=None, conflict=False)`. `in_progress` (with
    `conflict`): a request with this key is still running."""

    hit: CacheHit | None
    conflict: bool
    in_progress: bool = False


# The in-progress claim is renewed while the handler runs; a holder that dies
# frees the key after this.
IN_PROGRESS_TTL_S = 60
_releases = TaskSet()
# Under the Postgres authority: the (org, key, lease token) this request claimed and
# its operation; a memory commit of the operation marks the key committed in the same
# transaction.
_bound: ContextVar[tuple[str, str, int] | None] = ContextVar("loom_idempotency_key", default=None)
_bound_op: ContextVar[str] = ContextVar("loom_idempotency_op", default="")
# The operations whose answer `_committed_hit` rebuilds from their revisions. Another
# operation's commit (`profile.set` writes through memory.set) leaves its key running.
COMMITTED_REPLAY_OPS = frozenset({"memory.set", "memory.bulk_set"})


def bound_key() -> tuple[str, str, int] | None:
    """The claim a commit marks committed, when the request's operation replays one."""
    return _bound.get() if _bound_op.get() in COMMITTED_REPLAY_OPS else None


def unbind() -> None:
    """Forget the claim of the previous call in this context (an MCP batch runs its
    calls in one), so a call without a key commits under none."""
    _bound.set(None)
    _bound_op.set("")


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
    if pg.enabled():
        await _pg_store(org, idempotency_key, status_code, response_body, response_headers)
        return
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


# ---- the Postgres authority ----------------------------------------------------

_PG_READ = """
SELECT request_digest, state, operation, lease_token,
       coalesce(lease_until > now(), false) AS held, expires_at < now() AS expired,
       status_code, response_body, response_headers, revisions
FROM loom.idempotency WHERE org = $1 AND key_hash = $2 FOR UPDATE
"""


async def check_owned(owner: Scope, operation: str, body_hash: str) -> CacheResult:
    """`check` under the Postgres authority: the key lives in `loom.idempotency` beside
    the revisions it commits, and fails closed (Postgres down is 503). The key's row
    is read and claimed in one transaction.

    No row (or an expired one): this request claims it (`running`, a lease, token
    1) and runs. `done`: the stored answer. `committed`: the revisions a request
    committed before its answer was lost, replayed as such (`_committed_hit`).
    `running` or `committed` under a live lease: IDEMPOTENT_IN_PROGRESS. `running`
    past its lease: taken over under the next token, so the request it replaces
    can no longer commit under the key. Another body under the key: a conflict."""
    from server.core import request_tail

    in_request = request_tail.active()
    async with pg.transaction() as conn:
        # A claim made now is either fenced by an erasure's step or stamped after it; the
        # step's fence is brief, so a request that meets it is told to retry.
        if in_request and not await pg.try_claim_lock(conn, owner.org):
            return CacheResult(hit=None, conflict=True, in_progress=True)
        row = await conn.fetchrow(_PG_READ, owner.org, owner.key)
        if row is not None and row["expired"]:
            await conn.execute(
                "DELETE FROM loom.idempotency WHERE org = $1 AND key_hash = $2",
                owner.org,
                owner.key,
            )
            row = None
        if row is not None and row["request_digest"] != body_hash:
            return CacheResult(hit=None, conflict=True)
        if row is not None and row["state"] == "done":
            return CacheResult(hit=_done_hit(row), conflict=False)
        if row is not None and row["held"]:
            return CacheResult(hit=None, conflict=True, in_progress=True)
        if row is not None and row["state"] == "committed":
            return CacheResult(hit=await _committed_hit(conn, row), conflict=False)
        if not in_request:
            return CacheResult(hit=None, conflict=False)
        if row is None:
            token = await conn.fetchval(
                "INSERT INTO loom.idempotency (org, key_hash, subject, operation, "
                "request_digest, state, lease_until, expires_at, created_at) "
                "VALUES ($1, $2, $3, $4, $5, 'running', now() + make_interval(secs => $6), "
                "now() + make_interval(secs => $7), clock_timestamp()) "
                "ON CONFLICT (org, key_hash) DO NOTHING RETURNING lease_token",
                owner.org,
                owner.key,
                owner.subject,
                operation,
                body_hash,
                float(IN_PROGRESS_TTL_S),
                TTL.total_seconds(),
            )
            if token is None:  # claimed by a request that read no row either
                return CacheResult(hit=None, conflict=True, in_progress=True)
        else:
            token = await conn.fetchval(
                # The claim is this request's from now: an erasure's census reads
                # `created_at` (`steps_pg.pg_residue`).
                "UPDATE loom.idempotency SET lease_token = lease_token + 1, "
                "lease_until = now() + make_interval(secs => $3), created_at = clock_timestamp(), "
                "expires_at = now() + make_interval(secs => $4) "
                "WHERE org = $1 AND key_hash = $2 RETURNING lease_token",
                owner.org,
                owner.key,
                float(IN_PROGRESS_TTL_S),
                TTL.total_seconds(),
            )
    _bound.set((owner.org, owner.key, int(token)))
    _bound_op.set(operation)
    renewing = _releases.spawn(
        _pg_renew(owner.org, owner.key, int(token)), name="loom-idempotency-renew"
    )
    request_tail.defer(partial(_pg_release, owner.org, owner.key, int(token), renewing))
    return CacheResult(hit=None, conflict=False)


async def _pg_renew(org: str, key: str, token: int) -> None:
    """Keep this request's claim while it runs, so its retry does not take it over: its
    lease is extended every third of it, under the request's token, until the answer
    is stored or the claim released."""
    while True:
        await asyncio.sleep(IN_PROGRESS_TTL_S / 3)
        try:
            held = await pg.execute(
                "UPDATE loom.idempotency SET lease_until = now() + make_interval(secs => $4) "
                "WHERE org = $1 AND key_hash = $2 AND lease_token = $3 AND state <> 'done'",
                org,
                key,
                token,
                float(IN_PROGRESS_TTL_S),
            )
        except Exception:  # noqa: BLE001 — the lease runs out instead
            log.warning("idempotency: claim renewal failed for key=%s", key)
            continue
        if held == "UPDATE 0":
            return


def _done_hit(row: Any) -> CacheHit:
    return CacheHit(
        status_code=int(row["status_code"] or 200),
        body=row["response_body"],
        headers=dict(row["response_headers"] or {}),
    )


async def _committed_hit(conn: Any, row: Any) -> CacheHit:
    """What a request committed before its answer was lost, in its op's answer shape
    (only those of COMMITTED_REPLAY_OPS mark their keys committed): each revision's
    identity, the first one's at the top level for memory.set, one `written` result
    per row for memory.bulk_set, marked `idempotent_replay: committed`. Their
    ClickHouse delivery is the delivery worker's, as for any commit."""
    refs = list(row["revisions"] or [])
    revs = await conn.fetch(
        "SELECT r.namespace, r.seq, r.memory_id::text AS memory_id, r.version, "
        "r.content_sha256, r.row->>'memory_key' AS memory_key "
        "FROM loom.memory_revisions r "
        "JOIN unnest($1::text[], $2::text[], $3::bigint[]) AS k(org, namespace, seq) "
        "USING (org, namespace, seq) ORDER BY r.seq",
        [str(r[0]) for r in refs],
        [str(r[1]) for r in refs],
        [int(r[2]) for r in refs],
    )
    if len(revs) < len(refs):
        # An erasure removed some of what it committed (a batch across users or
        # namespaces may keep the rest): no answer names what is gone, so none is given.
        raise NotFoundError("the memory this request committed was erased")
    revisions = [
        {
            "namespace": r["namespace"],
            "memory_key": r["memory_key"],
            "memory_id": r["memory_id"],
            "version": int(r["version"]),
            "content_sha256": r["content_sha256"],
        }
        for r in revs
    ]
    body: dict[str, Any]
    if row["operation"] == "memory.bulk_set":  # its answer's own shape: one result per row
        body = {
            "written": [
                {k: r[k] for k in ("memory_key", "version", "memory_id")} for r in revisions
            ]
        }
    else:  # memory.set's: the row at the top level
        body = {**(revisions[0] if revisions else {}), "revisions": revisions}
    body["idempotent_replay"] = "committed"
    return CacheHit(status_code=200, body=body, headers={"X-Loom-Idempotent-Replay": "committed"})


async def _pg_store(
    org: str,
    key: str,
    status_code: int,
    response_body: Any,
    response_headers: dict[str, str] | None,
) -> None:
    bound = _bound.get()
    if bound is None or bound[:2] != (org, key):
        return
    try:
        # In a transaction: its commit waits for the standby, so a failover keeps the
        # answer and a retry replays it instead of running the op again.
        async with pg.transaction() as conn:
            await conn.execute(
                "UPDATE loom.idempotency SET state = 'done', status_code = $4, "
                "response_body = $5, response_headers = $6, lease_until = NULL "
                "WHERE org = $1 AND key_hash = $2 AND lease_token = $3",
                org,
                key,
                bound[2],
                status_code,
                # Through JSON once: a datetime or a model in the body becomes its text.
                json.loads(json.dumps(response_body, default=str)),
                response_headers or {},
            )
    except Exception as e:  # noqa: BLE001 — the answer stands; a retry gets the committed replay
        # Its type and code only: the error may quote the answer.
        log.warning("idempotency.store failed for key=%s: %s", key, error_summary(e))


async def sweep_expired(limit: int) -> int:
    """Delete up to `limit` idempotency keys past their retention, oldest first: a retry
    with one of them runs as a new request."""
    n = await pg.fetchval(
        "WITH gone AS (DELETE FROM loom.idempotency WHERE ctid IN ("
        "  SELECT ctid FROM loom.idempotency WHERE expires_at < now() "
        "  ORDER BY expires_at LIMIT $1 FOR UPDATE SKIP LOCKED) RETURNING 1) "
        "SELECT count(*) FROM gone",
        limit,
    )
    return int(n or 0)


async def _pg_release(org: str, key: str, token: int, renewing: Any = None) -> None:
    """A request that answered without a stored answer gives its claim up, so the next
    retry does not wait out the lease: one that never committed deletes it (the retry
    runs), one that committed (an error after its commit) ends its lease (the retry
    gets the committed replay)."""
    if renewing is not None:
        renewing.cancel()
    try:
        await pg.execute(
            "WITH gone AS (DELETE FROM loom.idempotency "
            "  WHERE org = $1 AND key_hash = $2 AND lease_token = $3 AND state = 'running') "
            "UPDATE loom.idempotency SET lease_until = now() "
            "WHERE org = $1 AND key_hash = $2 AND lease_token = $3 AND state = 'committed'",
            org,
            key,
            token,
        )
    except Exception:  # noqa: BLE001 — the lease runs out instead
        log.warning("idempotency: claim release failed for key=%s", key)
