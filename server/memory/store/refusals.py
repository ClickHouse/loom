"""Durable refusal policy and admission fences shared by writers and erasers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from datetime import UTC
from typing import Any

from server.core import lease_writes, pg
from server.core.ch import sequential_reads, set_query_tag, write_limit_s
from server.core.ch_params import chunks
from server.core.ch_writes import batched_insert_settings, committed_insert_settings
from server.core.datetimes import naive_utc_now
from server.core.errors import BackendUnavailableError, ValidationError
from server.core.hashing import hash_text, served_row_value, sha256_value
from server.core.repo_base import _CHClient
from server.memory.domain import refusals

# Protocol constant: erasers acquire every stripe; every publisher acquires one.
_STRIPES = 32
_writer: Callable[[], str] | None = None
_shared_permit: ContextVar[tuple[str, str, str, str] | None] = ContextVar(
    "shared_refusal_permit", default=None
)


_CH_SCOPE = (
    "((scope_kind IN ('identity','erased_identity') AND scope_value={ns:String} "
    "AND user_id={uid:String}) "
    "OR (scope_kind='namespace' AND scope_value={ns:String}) "
    "OR (scope_kind='user_id' AND scope_value={uid:String}))"
)
_PG_SCOPE = (
    "((scope_kind IN ('identity','erased_identity') AND scope_value=$2 AND user_id=$3) "
    "OR (scope_kind='namespace' AND scope_value=$2) "
    "OR (scope_kind='user_id' AND scope_value=$3))"
)

_CH_ERASURE_SCOPE = _CH_SCOPE.replace("('identity','erased_identity')", "('erased_identity')")
_PG_ERASURE_SCOPE = _PG_SCOPE.replace("('identity','erased_identity')", "('erased_identity')")


def _lease_client(ch: _CHClient) -> _CHClient:
    return ch


def _admission_wait_s() -> float:
    return write_limit_s()


def use_writer(writer: Callable[[], str]) -> None:
    global _writer
    _writer = writer


def _scope(kind: str, org: str, value: str) -> str:
    return json.dumps([kind, org, value], separators=(",", ":"))


def _name(scope: str, stripe: int) -> str:
    return "memory_refusal/" + hashlib.sha256(scope.encode()).hexdigest() + f"/{stripe}"


def fence_names(rows: Sequence[Mapping[str, Any]], *, forgetting: bool = False) -> list[str]:
    names: set[str] = set()
    for row in rows:
        org, ns, uid = str(row["org"]), str(row["namespace"]), str(row.get("user_id") or "-")
        encoded = json.dumps(
            [org, ns, row.get("agent"), uid, row.get("session_id"), row.get("memory_key")],
            separators=(",", ":"),
            default=str,
        )
        stripe = int.from_bytes(hashlib.sha256(encoded.encode()).digest()[:4]) % _STRIPES
        names.add(_name(_scope("org", org, ""), stripe))
        names.add(_name(_scope("namespace", org, ns), stripe))
        user_scope = _scope("user_id", org, uid)
        names.update(_name(user_scope, n) for n in (range(_STRIPES) if forgetting else [stripe]))
    return sorted(names)


def erasure_fence_names(org: str, kind: str, value: str) -> list[str]:
    return sorted(
        _name(scope, n)
        for scope in (_scope("org", org, ""), _scope(kind, org, value))
        for n in range(_STRIPES)
    )


def assert_shared_publication(row: Mapping[str, Any], *, vector: bool = False) -> None:
    if row.get("tombstoned_at") is not None:
        return
    digest = row.get("content_sha256") if vector else sha256_value(served_row_value(row))
    if _shared_permit.get() != (row["org"], row["namespace"], row["memory_key"], digest):
        raise ValidationError("A promotion requires its live source's admission")


@asynccontextmanager
async def source_publication(
    ch: _CHClient, source: Any, target_key: str, *, refusal_users: set[str] | None = None
) -> AsyncIterator[Callable[[bool], None]]:
    from server.memory.domain.temporal_sql import SERVED_STATUSES
    from server.memory.store import pg_memory
    from server.memory.store.reads import memory_rows_by_identity_keys
    from server.memory.store.rows import build_memory_version_row, identity_key

    if (
        source.status not in SERVED_STATUSES
        or source.requires_review
        or source.tombstoned_at
        or source.sensitivity == "high"
    ):
        raise ValidationError("Only an approved live source can be promoted")
    row = build_memory_version_row(source)
    key = identity_key(row)
    async with lease_writes.fenced_many(
        fence_names([row]), ch=_lease_client(ch), wait_s=_admission_wait_s()
    ) as mark:
        async with AsyncExitStack() as stack:
            conn = None
            if pg.enabled():
                durable = await pg_memory.heads_by_keys(
                    [key], include_tombstoned=True, durable=True
                )
                if not pg_memory.unchanged(source, durable.get(key)):
                    raise pg_memory.AdmissionContendedError("The promotion's source changed")
                conn = await stack.enter_async_context(pg.transaction())
                await lock_postgres(conn, [row])
                heads = await pg_memory.heads_on_connection(conn, [key], lock=True)
                await check_postgres(conn, [row])
            else:
                heads = {
                    k: build_memory_version_row(h)
                    for k, h in (
                        await memory_rows_by_identity_keys(
                            [key], ch=ch, sequential=True, include_tombstoned=True
                        )
                    ).items()
                }
                await check_clickhouse(ch, [row])
            if refusal_users is not None and (
                not refusal_users
                or await _shared_values_refused(
                    ch,
                    conn,
                    [{**row, "user_id": user or "-"} for user in refusal_users],
                    erasure_only=False,
                )
            ):
                raise _refused()
            if not pg_memory.unchanged(source, heads.get(key)):
                raise pg_memory.AdmissionContendedError("The promotion's source changed")
            token = _shared_permit.set(
                (source.org, source.namespace, target_key, sha256_value(source.value))
            )
            try:
                yield mark
            finally:
                _shared_permit.reset(token)


@asynccontextmanager
async def shared_retirement(ch: _CHClient, row: Mapping[str, Any]) -> AsyncIterator[bool]:
    """A delayed shared tombstone cannot restore bytes removed by erasure."""
    from server.memory.domain.promotion_source import promotion_users

    users = promotion_users(list(row.get("promoted_from") or []), row["org"], row["namespace"])
    base = content_policy_row(row, served_row_value(row))
    sources = [{**base, "user_id": user or "-"} for user in sorted(users)] if users else [base]
    async with (
        lease_writes.fenced_many(
            fence_names([base]), ch=_lease_client(ch), wait_s=_admission_wait_s()
        ) as mark,
        AsyncExitStack() as stack,
    ):
        conn = await stack.enter_async_context(pg.transaction()) if pg.enabled() else None
        if conn is not None:
            await lock_postgres(conn, [base])
        erased = await _shared_values_refused(ch, conn, sources)
        from server.memory.store.shared import fetch_shared

        head = await fetch_shared(
            row["org"], row["namespace"], row["memory_key"], strong=True, ch=ch
        )
        publish = bool(users) and head is not None and not erased
        mark(True)
        if not publish:
            await _delete_old_shared(ch, row)
            mark(False)
        yield publish
        mark(False)


async def _delete_old_shared(ch: _CHClient, row: Mapping[str, Any]) -> None:
    set_query_tag("memory.retire_orphan_shared")
    await ch.execute(
        "DELETE FROM loom.shared WHERE org={org:String} "
        "AND namespace={namespace:String} AND memory_key={memory_key:String} "
        "AND version < {version:UInt64}",
        params={c: row[c] for c in ("org", "namespace", "memory_key", "version")},
        settings={"lightweight_deletes_sync": 2},
    )


async def _shared_values_refused(
    ch: _CHClient, conn: Any, sources: Sequence[Mapping[str, Any]], *, erasure_only: bool = True
) -> bool:
    refused = False
    inspected = [refusals.inspect(served_row_value(sources[0]))]
    keys = {(str(row["org"]), str(row["namespace"]), str(row["user_id"])) for row in sources}
    with sequential_reads():
        for key in sorted(keys):
            set_query_tag("memory.shared_consent")
            refused |= (
                await _matches_postgres(conn, key, inspected, erasure_only=erasure_only)
                if conn is not None
                else await _matches_clickhouse(ch, key, inspected, erasure_only=erasure_only)
            )
    return refused


def _policy(
    rows: Sequence[Mapping[str, Any]],
    *,
    scope_kind: str = "identity",
    scope_value: str | None = None,
) -> list[dict[str, Any]]:
    if _writer is None:
        raise RuntimeError("Refusal policy writer is not wired")
    result = []
    for row in rows:
        if row.get("value_truncated"):
            raise ValidationError("A complete value is required to record refusal policy")
        value = served_row_value(row)
        if row.get("capture_kind") == "metadata" and value in (None, "", {}, []):
            continue
        fingerprints = (
            refusals.payload_fingerprints(value)
            if row.get("capture_kind") == "payload"
            else refusals.fingerprint(value)
        )
        for kind, units, digest in sorted(fingerprints):
            result.append(
                {
                    **{
                        c: str(row.get(c) or "-")
                        for c in ("org", "namespace", "agent", "user_id", "session_id")
                    },
                    "scope_kind": scope_kind,
                    "scope_value": scope_value
                    if scope_value is not None
                    else str(row["namespace"]),
                    "actor": _writer() or "process:memory_refusal",
                    "source_class": "derived",
                    "kind": kind,
                    "units": units,
                    "digest": digest,
                    "created_at": naive_utc_now(),
                }
            )
    return result


def _recording_policy(rows: Sequence[Mapping[str, Any]], **scope: Any) -> list[dict[str, Any]]:
    policy = _policy(rows, **scope)
    if scope.get("scope_kind") == "user_id":
        others = [r for r in rows if str(r.get("user_id") or "-") != scope.get("scope_value")]
        policy += _policy(others, scope_kind="erased_identity")
    return policy


def _live_groups(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[Any]]:
    groups: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        if row.get("tombstoned_at") is None:
            key = (str(row["org"]), str(row["namespace"]), str(row.get("user_id") or "-"))
            groups.setdefault(key, []).append(refusals.inspect(served_row_value(row)))
    return groups


def _candidate_batch(inspected: list[Any], sizes: list[int]) -> list[refusals.Fingerprint]:
    if -1 in sizes:
        raise BackendUnavailableError("Memory intake waits for the organization's erasure")
    found: set[refusals.Fingerprint] = set()
    for value in inspected:
        found.update(refusals.candidates(value, sizes))
    return sorted(found)


def _max_units(inspected: list[Any]) -> int:
    return max((len(words) for _, texts in inspected for words in texts), default=0)


def _refused() -> ValidationError:
    return ValidationError("The value is refused by a previous deletion", reason="forgotten")


def _check_groups(
    rows: Sequence[Mapping[str, Any]],
) -> Iterator[tuple[tuple[str, str, str], list[Any], bool]]:
    yield from ((key, values, False) for key, values in _live_groups(rows).items())
    dead = [{**r, "tombstoned_at": None} for r in rows if r.get("tombstoned_at") is not None]
    yield from ((key, values, True) for key, values in _live_groups(dead).items())


async def _matches_clickhouse(
    ch: _CHClient, key: tuple[str, str, str], inspected: list[Any], *, erasure_only: bool = False
) -> bool:
    scope = _CH_ERASURE_SCOPE if erasure_only else _CH_SCOPE
    params = {"org": key[0], "ns": key[1], "uid": key[2], "max": _max_units(inspected)}
    lengths = await ch.query_all(
        "SELECT DISTINCT toInt64(units) AS units FROM loom.memory_refusals "
        "WHERE org={org:String} AND " + scope + " AND kind='literal' AND units <= {max:UInt32} "
        "UNION ALL SELECT toInt64(-1) AS units FROM loom.erasure_requests FINAL "
        "WHERE org={org:String} AND status IN ('queued', 'in_progress', 'failed') "
        "AND NOT startsWith(failure_reason, 'max_attempts (') LIMIT 1",
        params=params,
    )
    candidates = _candidate_batch(inspected, [int(r["units"]) for r in lengths])
    for batch in chunks(candidates):
        match = await ch.query_all(
            "SELECT 1 FROM loom.memory_refusals WHERE org={org:String} AND "
            + scope
            + " AND (kind, units, digest) IN "
            "{candidates:Array(Tuple(String,UInt32,FixedString(64)))} LIMIT 1",
            params={**params, "candidates": batch},
        )
        if match:
            return True
    return False


async def check_clickhouse(ch: _CHClient, rows: Sequence[Mapping[str, Any]]) -> None:
    with sequential_reads():
        for key, inspected, erasure_only in _check_groups(rows):
            if await _matches_clickhouse(ch, key, inspected, erasure_only=erasure_only):
                raise _refused()


async def record_clickhouse(
    ch: _CHClient,
    rows: Sequence[Mapping[str, Any]],
    *,
    scope_kind: str = "identity",
    scope_value: str | None = None,
) -> None:
    policy = _recording_policy(rows, scope_kind=scope_kind, scope_value=scope_value)
    if policy:
        await ch.insert(
            "loom.memory_refusals", policy, settings={"async_insert": 0, "wait_for_async_insert": 1}
        )


async def lock_postgres(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    forgetting: bool = False,
    erasure: tuple[str, str, str] | None = None,
) -> None:
    """Lock scope admission before generation rows, including empty/new namespaces."""
    scopes: dict[str, bool] = {}
    orgs = {str(row["org"]) for row in rows}
    if erasure is not None:
        orgs.add(erasure[1])
    # Every transaction takes organization locks first, including multi-namespace erasure.
    for org in sorted(orgs):
        fn = "pg_advisory_xact_lock" if erasure is not None else "pg_advisory_xact_lock_shared"
        await conn.execute(f"SELECT {fn}(hashtextextended($1, 0))", _scope("org", org, ""))
    for row in rows:
        org = str(row["org"])
        scopes[_scope("namespace", org, str(row["namespace"]))] = False
        scopes[_scope("user_id", org, str(row.get("user_id") or "-"))] = forgetting
    if erasure is not None:
        scopes[_scope(*erasure)] = True
    for name, exclusive in sorted(scopes.items()):
        fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        await conn.execute(f"SELECT {fn}(hashtextextended($1, 0))", name)


async def _matches_postgres(
    conn: Any, key: tuple[str, str, str], inspected: list[Any], *, erasure_only: bool = False
) -> bool:
    scope = _PG_ERASURE_SCOPE if erasure_only else _PG_SCOPE
    lengths = await conn.fetch(
        "SELECT DISTINCT units::bigint AS units FROM loom.memory_refusals "
        "WHERE org=$1 AND " + scope + " AND kind='literal' AND units<=$4 "
        "UNION ALL SELECT -1::bigint FROM loom.erasure_ledger "
        "WHERE org=$1 AND completed_at IS NULL AND abandoned_at IS NULL",
        *key,
        _max_units(inspected),
    )
    candidates = _candidate_batch(inspected, [int(r["units"]) for r in lengths])
    for start in range(0, len(candidates), 2000):
        batch = candidates[start : start + 2000]
        match = await conn.fetchval(
            "SELECT 1 FROM loom.memory_refusals r JOIN unnest($4::text[], $5::integer[], "
            "$6::text[]) AS k(kind, units, digest) USING(kind, units, digest) "
            "WHERE r.org=$1 AND " + scope + " LIMIT 1",
            *key,
            [p[0] for p in batch],
            [p[1] for p in batch],
            [p[2] for p in batch],
        )
        if match:
            return True
    return False


async def check_postgres(
    conn: Any, rows: Sequence[Mapping[str, Any]], *, erasure_only: bool = False
) -> None:
    for key, inspected, retired in _check_groups(rows):
        if await _matches_postgres(conn, key, inspected, erasure_only=erasure_only or retired):
            raise _refused()


async def record_postgres(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    scope_kind: str = "identity",
    scope_value: str | None = None,
) -> None:
    policy = _recording_policy(rows, scope_kind=scope_kind, scope_value=scope_value)
    if policy:
        for row in policy:
            row["created_at"] = row["created_at"].replace(tzinfo=UTC)
        columns = tuple(policy[0])
        placeholders = ",".join(f"${n}" for n in range(1, len(columns) + 1))
        await conn.executemany(
            f"INSERT INTO loom.memory_refusals ({','.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING",
            [tuple(row[c] for c in columns) for row in policy],
        )


def content_policy_row(row: Mapping[str, Any], value: Any) -> dict[str, Any]:
    return {
        **{
            c: str(row.get(c) or "-")
            for c in ("org", "namespace", "agent", "user_id", "session_id")
        },
        "memory_key": str(row.get("memory_key") or row.get("parent_memory_key") or "-"),
        "value_json": json.dumps(value, default=str),
        "value": json.dumps(value, default=str),
        "tombstoned_at": row.get("tombstoned_at"),
    }


async def check_intake(ch: _CHClient, row: Mapping[str, Any], value: Any) -> None:
    policy = [content_policy_row(row, value)]
    if pg.enabled():
        async with pg.transaction() as conn:
            await lock_postgres(conn, policy)
            await check_postgres(conn, policy)
    else:
        await check_clickhouse(ch, policy)


def _raw_content_values(row: Mapping[str, Any]) -> list[Any]:
    values = [
        row[c]
        for c in ("content", "caption", "source_url", "source_local_path")
        if row.get(c) not in (None, "")
    ]
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            pass
    if metadata not in (None, "", {}, []):
        values.append(metadata)
    return values


async def insert_content_rows(ch: _CHClient, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Refuse raw content under the same policy fences as canonical memory."""
    if table not in {"loom.memory_evidence", "loom.memory_chunks"}:
        raise ValueError("unsupported refusal-protected content table")
    policy = [content_policy_row(row, _raw_content_values(row)) for row in rows]
    if policy:
        async with publication(ch, policy):
            if table == "loom.memory_evidence":
                await ch.insert(
                    "loom.memory_evidence",
                    [dict(r) for r in rows],
                    settings=batched_insert_settings(),
                )
            else:
                await ch.insert(
                    "loom.memory_chunks",
                    [dict(r) for r in rows],
                    settings=committed_insert_settings(),
                )


async def _embedding_sources(
    ch: _CHClient, rows: Sequence[Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    from server.memory.store import pg_memory
    from server.memory.store.reads import memory_rows_by_identity_keys
    from server.memory.store.rows import build_memory_version_row, identity_key

    keys = [identity_key(r) for r in rows]
    if pg.enabled():
        heads = await pg_memory.heads_by_keys(keys, include_tombstoned=True, durable=True)
    else:
        found = (
            await memory_rows_by_identity_keys(
                keys, ch=ch, sequential=True, include_tombstoned=True
            )
            if keys
            else {}
        )
        heads = {k: build_memory_version_row(h) for k, h in found.items()}
    accepted: list[Mapping[str, Any]] = []
    sources: list[Mapping[str, Any]] = []
    for row in rows:
        head = heads.get(identity_key(row))
        if row.get("tombstoned_at") is not None:
            accepted.append(row)
            if head is not None:
                sources.append(head)
            continue
        if (
            head is not None
            and head.get("tombstoned_at") is None
            and hash_text(row.get("content_sha256")) == sha256_value(served_row_value(head))
            and row.get("status", "active") == head.get("status", "active")
        ):
            accepted.append(row)
            sources.append(head)
    return accepted, sources


def _publication_values(
    sources: Sequence[Mapping[str, Any]], admitted: Sequence[Mapping[str, Any]], *, embeddings: bool
) -> list[Mapping[str, Any]]:
    from server.memory.store.rows import identity_key

    admitted_keys = {identity_key(r) for r in admitted}
    values = [s for s in sources if not embeddings or identity_key(s) in admitted_keys]
    if embeddings:
        values.extend(
            content_policy_row(row, row["content_excerpt"])
            for row in admitted
            if row.get("tombstoned_at") is None and row.get("content_excerpt")
        )
    return values


@asynccontextmanager
async def publication(
    ch: _CHClient,
    rows: Sequence[Mapping[str, Any]],
    *,
    embeddings: bool = False,
    committed: bool = False,
    retired: list[dict[str, Any]] | None = None,
) -> AsyncIterator[Sequence[Mapping[str, Any]]]:
    """Hold the policy admission through the complete memory/vector send."""
    async with lease_writes.fenced_many(
        fence_names(rows), ch=_lease_client(ch), wait_s=_admission_wait_s()
    ) as mark:
        if pg.enabled():
            from server.memory.store import pg_memory
            from server.memory.store.rows import identity_key

            # A durable read must finish before any bytes it returned can be published.
            admitted, sources = await _embedding_sources(ch, rows) if embeddings else (rows, rows)
            async with pg.transaction() as conn:
                await lock_postgres(conn, rows)
                if embeddings:
                    current = await pg_memory.heads_on_connection(
                        conn, [identity_key(r) for r in rows], lock=True
                    )
                    unchanged = {
                        identity_key(r) for r in sources if current.get(identity_key(r)) == r
                    }
                    admitted = [
                        r
                        for r in admitted
                        if identity_key(r) in unchanged
                        or (
                            r.get("tombstoned_at") is not None
                            and identity_key(r) not in current
                            and identity_key(r) not in {identity_key(s) for s in sources}
                        )
                    ]
                    sources = [r for r in sources if identity_key(r) in unchanged]
                if embeddings:
                    admitted = await _embedding_retirements(
                        ch, admitted, sources, mark, retired, conn=conn
                    )
                await check_postgres(
                    conn,
                    _publication_values(sources, admitted, embeddings=embeddings),
                    # Authority commits enforce point refusal before becoming history.
                    erasure_only=committed and not embeddings,
                )
                mark(True)
                yield admitted
                mark(False)
        else:
            admitted, sources = await _embedding_sources(ch, rows) if embeddings else (rows, rows)
            if embeddings:
                admitted = await _embedding_retirements(ch, admitted, sources, mark, retired)
            await check_clickhouse(
                ch, _publication_values(sources, admitted, embeddings=embeddings)
            )
            mark(True)
            yield admitted
            mark(False)


async def _embedding_retirements(
    ch: _CHClient,
    rows: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    mark: Callable[[bool], None],
    retired: list[dict[str, Any]] | None,
    *,
    conn: Any = None,
) -> list[Mapping[str, Any]]:
    from server.memory.domain.redaction_lineage import is_content_hash
    from server.memory.store.embeddings import _bare_tombstone
    from server.memory.store.rows import identity_key

    heads = {identity_key(r): r for r in sources}
    accepted: list[Mapping[str, Any]] = []
    for row in rows:
        if row.get("tombstoned_at") is None:
            accepted.append(row)
            continue
        head = heads.get(identity_key(row))
        values = [served_row_value(head)] if head is not None else []
        if row.get("content_excerpt"):
            values.append(row["content_excerpt"])
        inspected = refusals.inspect(values)
        digest = hash_text(row.get("content_sha256"))
        if is_content_hash(digest):
            inspected[0].add(("value", 0, digest))
        key = (str(row["org"]), str(row["namespace"]), str(row.get("user_id") or "-"))
        with sequential_reads():
            erased = (
                await _matches_postgres(conn, key, [inspected], erasure_only=True)
                if conn is not None
                else await _matches_clickhouse(ch, key, [inspected], erasure_only=True)
            )
            if head is None or erased:
                mark(True)
                await _delete_old_vector(ch, row)
                mark(False)
                if retired is not None:
                    retired.append(dict(row))
                continue
            refused = (
                await _matches_postgres(conn, key, [inspected])
                if conn is not None
                else await _matches_clickhouse(ch, key, [inspected])
            )
        accepted.append(
            _bare_tombstone(dict(row), int(row["version"]), naive_utc_now()) if refused else row
        )
    return accepted


async def _delete_old_vector(ch: _CHClient, row: Mapping[str, Any]) -> None:
    """Clean an orphan without introducing a row after a physical erasure."""
    set_query_tag("memory.retire_orphan_vector")
    await ch.execute(
        "DELETE FROM loom.memory_embeddings WHERE org={org:String} "
        "AND namespace={namespace:String} AND agent={agent:String} AND user_id={user_id:String} "
        "AND session_id={session_id:String} AND scope={scope:String} "
        "AND embedding_model={embedding_model:String} AND memory_key={memory_key:String} "
        "AND version < {version:UInt64}",
        params={
            c: row[c]
            for c in (
                "org",
                "namespace",
                "agent",
                "user_id",
                "session_id",
                "scope",
                "embedding_model",
                "memory_key",
                "version",
            )
        },
        settings={"lightweight_deletes_sync": 2},
    )
