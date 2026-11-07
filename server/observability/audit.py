"""Audit log writer with per-row SHA-256 tamper hashes.

Each row carries `row_hash = SHA-256(canonical(row))`; recomputing it at
read time detects content tampering. This is per-row tamper detection, NOT
a hash chain — chaining to a predecessor forks under concurrent writers
without a single-leader writer, which a stateless Control Plane isn't.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from server.core.ch import get_client
from server.core.ch_writes import batched_insert_settings
from server.observability.instance import stamp_row

log = logging.getLogger(__name__)


def _canonical(row: dict[str, Any]) -> bytes:
    """Deterministic byte serialization. Sorted keys, ASCII-safe JSON."""
    return json.dumps(row, sort_keys=True, default=str, ensure_ascii=True).encode("utf-8")


def _to_str(v: Any) -> str:
    """ClickHouse FixedString(N) round-trips as `bytes` via clickhouse-connect.
    Normalize to str so hash comparisons work."""
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return str(v)


def _row_hash(row: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(_canonical(row))
    return h.hexdigest()


def _normalize_ts(ts: datetime) -> datetime:
    """Return tz-aware UTC ts with millisecond precision.

    clickhouse-connect interprets *naive* datetimes as the client's local
    tz on insert, which would silently shift the stored value if the
    client is not in UTC. We insert tz-aware UTC, and use
    `_ts_canonical_form()` to derive the deterministic string for hashing.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    else:
        ts = ts.astimezone(UTC)
    ms = (ts.microsecond // 1000) * 1000
    return ts.replace(microsecond=ms)


def _ts_canonical_form(ts: datetime) -> str:
    """The string used inside the canonical-JSON hash payload.

    `DateTime64(3)` round-trips as a naive Python datetime in some
    clickhouse-connect versions and tz-aware in others. Strip to naive
    UTC at ms precision on both write and read so the hash agrees.
    """
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    ms = (ts.microsecond // 1000) * 1000
    return ts.replace(microsecond=ms).isoformat()


async def emit(
    event_type: str,
    *,
    org: str,
    namespace: str,
    actor: str,
    subject: dict[str, Any] | list[Any] | str | None = None,
    request_id: str = "",
    ts: datetime | None = None,
    consumer: str | None = None,
) -> str:
    """Write one audit row. Returns the row_hash. Never raises (errors logged).

    `consumer`: the client the request spoke for; None reads
    the request-scoped value the transport published, so every audit row a
    request produces carries it without per-call plumbing. Outside the hash
    payload, like the build/instance stamps."""
    try:
        return await emit_strict(
            event_type,
            org=org,
            namespace=namespace,
            actor=actor,
            subject=subject,
            request_id=request_id,
            ts=ts,
            consumer=consumer,
        )
    except Exception as e:  # fail open: an audit write never fails the op it records
        log.exception("audit.emit failed for event_type=%s: %s", event_type, e)
        return ""


async def emit_strict(
    event_type: str,
    *,
    org: str,
    namespace: str,
    actor: str,
    subject: dict[str, Any] | list[Any] | str | None = None,
    request_id: str = "",
    ts: datetime | None = None,
    consumer: str | None = None,
) -> str:
    """`emit` that raises when the row is not written: for a record an op depends on
    (an Undo's, which the conflict resolver reads), written before the op's change."""
    row = _audit_row(
        event_type,
        org,
        namespace,
        actor,
        subject,
        request_id=request_id,
        ts=ts,
        consumer=consumer,
    )
    await get_client().insert("loom.audit_log", [row], settings=batched_insert_settings())
    return str(row["row_hash"])


async def emit_many(
    event_type: str,
    *,
    org: str,
    namespace: str,
    actor: str,
    subjects: list[dict[str, Any]],
) -> None:
    """`emit` for several subjects of one event, in one insert. Never raises."""
    if not subjects:
        return
    try:
        rows = [_audit_row(event_type, org, namespace, actor, s) for s in subjects]
        await get_client().insert("loom.audit_log", rows, settings=batched_insert_settings())
    except Exception as e:  # fail open: an audit write never fails the op it records
        log.exception("audit.emit_many failed for event_type=%s: %s", event_type, e)


# Input fields whose text an audit subject keeps: identity, principals, ids, keys and
# URIs, which name what a call acted on and who acted. Every other text field (a value, a
# reason, a payload) is kept as its SHA-256 and length, so `loom.audit_log` holds no text
# an erasure must reach.
_NAMING_FIELDS = frozenset(
    {
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "run_id",
        "thread_id",
        "subject",
        "grantee",
        "owner_email",
        "requester",
        "approver",
        "key",
        "keys",
        "name",
    }
)
_NAMING_SUFFIXES = (
    "_id",
    "_ids",
    "_key",
    "_keys",
    "_uri",
    "_uris",
    "_namespace",
    "_org",
    "_agent",
    "_name",
    "_by",
)


def _is_vocabulary(annotation: Any) -> bool:
    """True for a Literal or Enum annotation, alone, in a list or inside Optional/Union."""
    origin = get_origin(annotation)
    if origin is Literal:
        return True
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return True
    if origin in (Union, UnionType):
        return all(a is type(None) or _is_vocabulary(a) for a in get_args(annotation))
    if origin in (list, tuple, set, frozenset):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        return bool(args) and all(_is_vocabulary(a) for a in args)
    return False


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The model a field holds, alone or inside Optional."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, UnionType):
        models = [m for m in map(_nested_model, get_args(annotation)) if m is not None]
        return models[0] if len(models) == 1 else None
    return None


def _digest(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "chars": len(text)}


def _shape(model: type[BaseModel], dumped: dict[str, Any]) -> dict[str, Any]:
    fields = model.model_fields
    subject: dict[str, Any] = {}
    for name, value in dumped.items():
        field = fields.get(name)
        annotation = field.annotation if field is not None else None
        nested = _nested_model(annotation)
        if nested is not None and isinstance(value, dict):
            # A free-form dict (a payload, a record's fields) has no model and is hashed.
            subject[name] = _shape(nested, value)
            continue
        keep = (
            value in (None, "", [], {})
            or isinstance(value, bool | int | float)
            or name in _NAMING_FIELDS
            or name.endswith(_NAMING_SUFFIXES)
            or (annotation is not None and _is_vocabulary(annotation))
        )
        subject[name] = value if keep else _digest(value)
    return subject


def input_subject(input_obj: BaseModel) -> dict[str, Any]:
    """The audit subject of a validated op input that declares no `audit_subject`:
    numbers, booleans, Literal or Enum values and the naming fields verbatim, a nested
    model shaped by the same rule, and every other value (a free-form dict included) as
    its SHA-256 and length."""
    return _shape(type(input_obj), input_obj.model_dump(mode="json"))


def _audit_row(
    event_type: str,
    org: str,
    namespace: str,
    actor: str,
    subject: dict[str, Any] | list[Any] | str | None,
    *,
    request_id: str = "",
    ts: datetime | None = None,
    consumer: str | None = None,
) -> dict[str, Any]:
    # A subject whose keys sort_keys cannot order (mixed str/int) raises TypeError;
    # the callers catch it so it never reaches the write path.
    ts_norm = _normalize_ts(ts or datetime.now(UTC))
    subject_json = json.dumps(subject if subject is not None else {}, sort_keys=True, default=str)
    canonical = {
        "ts": _ts_canonical_form(ts_norm),
        "event_type": event_type,
        "org": org,
        "namespace": namespace,
        "actor": actor,
        "subject": subject_json,
        "request_id": request_id,
    }
    row = {
        "ts": ts_norm,
        "event_type": event_type,
        "org": org,
        "namespace": namespace,
        "actor": actor,
        "subject": subject_json,
        "request_id": request_id,
        "row_hash": _row_hash(canonical),
    }
    # Which build, process and launcher wrote the
    # row; outside the hash payload.
    stamp_row(row)
    from server.observability.consumer import current_consumer

    row["consumer"] = consumer if consumer is not None else current_consumer()
    # The credential that made the request (jti, else the
    # token's sha256 prefix — server/auth/jwt.py::token_id_of), outside
    # the hash payload like consumer; '' outside a request.
    row["token_id"] = _current_token_id()
    return row


def _current_token_id() -> str:
    try:
        from server.auth.context import current_auth

        auth = current_auth()
    except Exception:  # noqa: BLE001 — a ledger stamp never fails the write
        return ""
    return str(getattr(auth, "token_id", "") or "") if auth is not None else ""


def row_matches_hash(row: dict[str, Any]) -> bool:
    """True if `row`'s stored `row_hash` matches a recompute over its
    canonical content. Single source of truth for tamper detection —
    `verify_audit_hashes()`, the audit_verifier worker, and the dev CLI all
    call this. `ts` may be a datetime or an already-canonical string.
    """
    ts_str = _ts_canonical_form(row["ts"]) if isinstance(row["ts"], datetime) else str(row["ts"])
    stored = _to_str(row["row_hash"]).rstrip("\x00")
    canonical = {
        "ts": ts_str,
        "event_type": row["event_type"],
        "org": row["org"],
        "namespace": row["namespace"],
        "actor": row["actor"],
        "subject": row["subject"],
        "request_id": row["request_id"],
    }
    return _row_hash(canonical) == stored


async def verify_audit_hashes(
    *,
    org: str | None = None,
    namespace: str | None = None,
    partition_yyyymm: int | None = None,
) -> tuple[int, int]:
    """Walk rows in the filtered window and count (rows, tampered)."""
    ch = get_client()
    where: list[str] = []
    params: dict[str, Any] = {}
    if org is not None:
        where.append("org = {org:String}")
        params["org"] = org
    if namespace is not None:
        where.append("namespace = {namespace:String}")
        params["namespace"] = namespace
    if partition_yyyymm is not None:
        where.append("toYYYYMM(ts) = {p:UInt32}")
        params["p"] = partition_yyyymm
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    rows = await ch.query_all(
        f"""
        SELECT ts, event_type, org, namespace, actor, subject,
               request_id, row_hash
        FROM loom.audit_log
        {where_sql}
        ORDER BY ts ASC
        """,
        params=params,
    )

    tampered = sum(1 for row in rows if not row_matches_hash(row))
    return (len(rows), tampered)
