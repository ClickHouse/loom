"""Verify every digest byte against its authoritative, currently served sources."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from server.core import lease_writes, pg
from server.core.ch import sequential_reads
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient
from server.memory.domain.sensitivity import value_reads_high
from server.memory.domain.summary_provenance import render_summary, source_text
from server.memory.store import pg_memory
from server.memory.store.reads import memory_rows_by_identity_keys
from server.memory.store.rows import IdentityKey, MemoryRow, _hydrate


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return tuple(str(row.get(c) or "-") for c in ("agent", "user_id", "session_id"))  # type: ignore[return-value]


def _complete(row: dict[str, Any]) -> bool:
    keys, facts = row.get("derived_from"), row.get("key_facts")
    return bool(
        isinstance(keys, list)
        and isinstance(facts, list)
        and keys
        and len(keys) == len(facts)
        and all(isinstance(k, str) and k for k in keys)
        and len(set(keys)) == len(keys)
        and all(isinstance(f, str) and f for f in facts)
        and row.get("summary_text") == render_summary(facts)
        and not value_reads_high(row["summary_text"])
    )


async def _source_heads(
    ch: _CHClient,
    rows: list[dict[str, Any]],
    *,
    org: str,
    namespace: str,
    durable: bool = False,
    conn: Any = None,
) -> dict[IdentityKey, MemoryRow]:
    candidates = [r for r in rows if _complete(r)]
    keys = sorted(
        {(org, namespace, *_identity(r), key) for r in candidates for key in r["derived_from"]}
    )
    if not keys:
        return {}
    # A JSON string may use two UTF-16 escapes per code point, plus its quotes.
    cap = 12 * max(len(f) for r in candidates for f in r["key_facts"]) + 2
    if pg.enabled():
        if conn is not None:
            found = await pg_memory.heads_on_connection(conn, keys, lock=True, max_value_chars=cap)
        else:
            found = await pg_memory.heads_by_keys(keys, durable=durable, max_value_chars=cap)
        return {key: _hydrate(row) for key, row in found.items()}
    with sequential_reads():
        return await memory_rows_by_identity_keys(keys, ch=ch, sequential=True, max_value_chars=cap)


def _verified(
    rows: list[dict[str, Any]],
    heads: Mapping[IdentityKey, MemoryRow],
    *,
    org: str,
    namespace: str,
    as_of: Any = None,
    as_of_basis: str = "valid_time",
) -> list[dict[str, Any]]:
    now = naive_utc_now()
    result = []
    for row in rows:
        if not _complete(row):
            continue
        texts = []
        for key in row["derived_from"]:
            source = heads.get((org, namespace, *_identity(row), key))
            text = (
                source_text(source, now=now, as_of=as_of, as_of_basis=as_of_basis)
                if source
                else None
            )
            if text is None:
                break
            texts.append(text)
        if texts == row["key_facts"]:
            result.append(row)
    return result


async def current_summary_sources(
    ch: _CHClient,
    rows: list[dict[str, Any]],
    *,
    org: str,
    namespace: str,
    as_of: Any = None,
    as_of_basis: str = "valid_time",
) -> list[dict[str, Any]]:
    """Withhold missing, unverifiable and rewritten lineage, including legacy digests."""
    heads = await _source_heads(ch, rows, org=org, namespace=namespace)
    return _verified(
        rows, heads, org=org, namespace=namespace, as_of=as_of, as_of_basis=as_of_basis
    )


async def verified_summary_batch(ch: _CHClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verify a worker batch without mixing source identities across tenants."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("org") and row.get("namespace"):
            groups[(str(row["org"]), str(row["namespace"]))].append(row)
    verified: set[int] = set()
    for (org, namespace), batch in groups.items():
        verified.update(
            id(r) for r in await current_summary_sources(ch, batch, org=org, namespace=namespace)
        )
    return [row for row in rows if id(row) in verified]


async def insert_verified_summaries(ch: _CHClient, rows: list[dict[str, Any]]) -> int:
    """Publish only current proven text, fenced against deletion and erasure."""
    from server.memory.store import refusals

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["org"]), str(row["namespace"]))].append(row)
    written = 0
    for (org, namespace), batch in groups.items():
        policy = [
            refusals.content_policy_row(r, [r["summary_text"], *r["key_facts"]]) for r in batch
        ]
        async with lease_writes.fenced_many(
            refusals.fence_names(policy),
            ch=refusals._lease_client(ch),
            wait_s=refusals._admission_wait_s(),
        ) as mark:
            async with AsyncExitStack() as stack:
                if pg.enabled():
                    durable = await _source_heads(
                        ch, batch, org=org, namespace=namespace, durable=True
                    )
                    conn = await stack.enter_async_context(pg.transaction())
                    await refusals.lock_postgres(conn, policy)
                    current = await _source_heads(
                        ch, batch, org=org, namespace=namespace, conn=conn
                    )
                    heads = {
                        key: row
                        for key, row in current.items()
                        if key in durable and pg_memory.unchanged(durable[key], row)
                    }
                else:
                    heads = await _source_heads(ch, batch, org=org, namespace=namespace)
                admitted = _verified(batch, heads, org=org, namespace=namespace)
                admitted_policy = [
                    refusals.content_policy_row(r, [r["summary_text"], *r["key_facts"]])
                    for r in admitted
                ]
                if pg.enabled():
                    await refusals.check_postgres(conn, admitted_policy)
                else:
                    await refusals.check_clickhouse(ch, admitted_policy)
                if admitted:
                    mark(True)
                    await ch.insert(
                        "loom.session_summaries",
                        admitted,
                        settings={"async_insert": 0, "wait_for_async_insert": 1},
                    )
                    mark(False)
                    written += len(admitted)
    return written
