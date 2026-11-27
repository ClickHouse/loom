"""Record refusal fingerprints before an erasure removes its source history."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core import lease_writes, pg
from server.core.ch import sequential_reads, set_query_tag, write_limit_s
from server.core.ch_params import chunks
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.consent import current_policy
from server.erasure.store.predicates import (
    USER_CHUNKS,
    USER_SUMMARIES,
    USER_SUMMARY_CHUNKS,
    USER_SUMMARY_VECTORS,
)

_KEYS = (
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "version",
    "body_sha",
)
_CURSOR = "Tuple(String,String,String,String,String,UInt64,FixedString(64))"
_BODY_SHA = "hex(SHA256(toJSONString((value_json, value)))) AS body_sha"


def _sources() -> list[tuple[str, str]]:
    identity = "org, namespace, agent, user_id, session_id, 'process:erasure' AS actor, "
    version = "toUInt64(toUnixTimestamp64Nano(toDateTime64(created_at, 9))) AS version, "
    sources = [
        (
            "loom.session_summaries",
            identity + "concat('summary:', session_id) AS memory_key, version, "
            "'{}' AS value_json, "
            "toJSONString(arrayJoin(arrayConcat([summary_text], key_facts))) AS value",
        ),
        (
            "loom.memory_evidence",
            identity
            + "concat('evidence:', toString(evidence_id)) AS memory_key, "
            + version
            + "'{}' AS value_json, toJSONString(content) AS value",
        ),
        (
            "loom.memory_evidence",
            identity
            + "concat('evidence-metadata:', toString(evidence_id)) AS memory_key, "
            + version
            + "'{}' AS value_json, metadata AS value, 'metadata' AS capture_kind",
        ),
        (
            "loom.memory_chunks",
            identity + "concat('chunk:', toString(chunk_index), ':', "
            "content_sha256) AS memory_key, " + version + "'{}' AS value_json, "
            "toJSONString(arrayJoin([content, caption, source_url, source_local_path])) AS value",
        ),
    ]
    sources.append(
        (
            "loom.memory_embeddings",
            identity + "memory_key, version, '{}' AS value_json, "
            "toJSONString(content_excerpt) AS value",
        )
    )
    sources.extend(
        [
            (
                "loom.memory_extraction_queue",
                identity + "concat('queue:', queue_id) AS memory_key, version, "
                "'{}' AS value_json, payload AS value, 'payload' AS capture_kind",
            ),
            (
                "loom.memory_extraction_queue",
                identity + "concat('queue-error:', queue_id) AS memory_key, version, "
                "'{}' AS value_json, toJSONString(last_error) AS value",
            ),
            (
                "loom.entity_summaries",
                "org, namespace, agent, user_id, '-' AS session_id, 'process:erasure' AS actor, "
                "concat('entity:', entity) AS memory_key, version, '{}' AS value_json, "
                "toJSONString(arrayJoin(arrayConcat([summary_text], key_facts))) AS value",
            ),
        ]
    )
    # Cleared native revision payloads survive in ClickHouse history.
    sources.insert(
        0,
        (
            "loom.memory_versions",
            identity + "memory_key, version, toString(value_json) AS value_json, value",
        ),
    )
    return sources


def _predicate(table: str, kind: str) -> str:
    if kind == "user_id":
        if table == "loom.memory_embeddings":
            return f"(user_id={{uid:String}} OR {USER_SUMMARY_VECTORS})"
        if table == "loom.memory_chunks":
            return f"({USER_CHUNKS} OR {USER_SUMMARY_CHUNKS})"
        if table == "loom.memory_versions":
            return f"(user_id={{uid:String}} OR {USER_SUMMARIES})"
    return f"{kind}={{scope:String}}"


async def _capture(org: str, kind: str, value: str) -> int:
    ch = _default_client()
    refusals = current_policy()
    names = refusals.fence_names(org, kind, value)
    count = 0
    # The ledger blocks new intake. Drain admitted sends before paging history;
    # capture can outlast a send lease without admitting new writers.
    async with lease_writes.fenced_many(
        names, ch=refusals.lease_client(ch), wait_s=write_limit_s()
    ):
        pass
    with sequential_reads():
        for table, projection in _sources():
            count += await _capture_source(ch, table, projection, org, kind, value)
    return count


async def _capture_source(
    ch: _CHClient, table: str, projection: str, org: str, kind: str, value: str
) -> int:
    refusals = current_policy()
    count = 0
    after: tuple[Any, ...] | None = None
    while True:
        set_query_tag("erasure.memory_refusals")
        predicates = []
        if after:
            predicates.append(f"({', '.join(_KEYS)}) > {{after:{_CURSOR}}}")
        if table != "loom.memory_versions":
            predicates.append("value != {empty_text:String}")
        cursor = "WHERE " + " AND ".join(predicates) if predicates else ""
        rows = await ch.query_all(
            f"SELECT *, {_BODY_SHA} FROM (SELECT {projection} FROM {table} "
            f"WHERE org={{org:String}} AND {_predicate(table, kind)}) {cursor} "
            f"ORDER BY {', '.join(_KEYS)} LIMIT 1000",
            params={
                "org": org,
                "scope": value,
                "uid": value,
                "after": after,
                "empty_text": '""',
            },
        )
        if not rows:
            break
        if pg.enabled():
            async with pg.transaction() as conn:
                await refusals.record_postgres(conn, rows, scope_kind=kind, scope_value=value)
        else:
            await refusals.record_clickhouse(ch, rows, scope_kind=kind, scope_value=value)
        count += len(rows)
        after = tuple(rows[-1][c] for c in _KEYS)
    return count


async def capture_user(org: str, user_id: str) -> int:
    return await _capture(org, "user_id", user_id)


async def capture_namespace(org: str, namespace: str | Sequence[str]) -> int:
    count = 0
    for ns in [namespace] if isinstance(namespace, str) else sorted(set(namespace)):
        count += await _capture(org, "namespace", ns)
    return count


async def capture_native_chunks(conn: Any, sources: list[dict[str, Any]], user_id: str) -> None:
    keys = sorted(
        {
            tuple(
                str(r.get(c) or "-") for c in ("org", "namespace", "agent", "user_id", "memory_key")
            )
            for r in sources
        }
    )
    for part in chunks(keys, limit=16 * 1024):
        await _capture_native_chunk_keys(conn, part, user_id)


async def _capture_native_chunk_keys(
    conn: Any, sources: list[tuple[str, ...]], user_id: str
) -> None:
    """Capture raw dependents by authoritative keys before native history disappears."""
    if not sources:
        return
    org = sources[0][0]
    identities = sorted(
        {
            (ns, agent, uid, key)
            for _, ns, agent, source_uid, key in sources
            for uid in (source_uid, "-")
        }
    )
    legacy = sorted({(ns, key) for _, ns, _, source_uid, key in sources if source_uid == user_id})
    ch = _default_client()
    for table, parent in (
        ("loom.memory_chunks", "parent_memory_key"),
        ("loom.memory_embeddings", "memory_key"),
    ):
        await _capture_native_dependents(conn, org, user_id, identities, legacy, table, parent, ch)


async def _capture_native_dependents(
    conn: Any,
    org: str,
    user_id: str,
    identities: Sequence[tuple[str, ...]],
    legacy: Sequence[tuple[str, ...]],
    table: str,
    parent: str,
    ch: _CHClient,
) -> None:
    projection = dict(_sources())[table]
    refusals = current_policy()
    after: tuple[Any, ...] | None = None
    with sequential_reads():
        while True:
            set_query_tag("erasure.memory_refusals")
            cursor = f"AND ({', '.join(_KEYS)}) > {{after:{_CURSOR}}}" if after else ""
            rows = await ch.query_all(
                f"SELECT *, {_BODY_SHA} FROM (SELECT {projection} FROM {table} "
                f"WHERE org={{org:String}} AND ((namespace, agent, user_id, {parent}) "
                "IN {identities:Array(Tuple(String,String,String,String))} OR "
                f"(user_id='-' AND (namespace, {parent}) "
                "IN {legacy:Array(Tuple(String,String))}))) "
                f"WHERE value != {{empty_text:String}} {cursor} "
                f"ORDER BY {', '.join(_KEYS)} LIMIT 1000",
                params={
                    "org": org,
                    "identities": identities,
                    "legacy": legacy,
                    "after": after,
                    "empty_text": '""',
                },
            )
            if not rows:
                return
            await refusals.record_postgres(conn, rows, scope_kind="user_id", scope_value=user_id)
            after = tuple(rows[-1][c] for c in _KEYS)
