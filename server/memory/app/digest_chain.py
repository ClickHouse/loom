"""Consecutive session digests of one identity are linked in time.

A session digest (`loom.session_summaries`, one row per session) is loom's
event-sized unit: a span of turns, a date, a summary, an identity. Without
links, the digests of one user are unrelated rows and the only order between
them is a timestamp the reader has to compare itself. Typed relations between
events help multi-hop and temporal questions, and the cheapest one is known
without a model: temporal order.

This module writes that order as authored edges in `loom.operator_edges`
(the table `memory.link` writes and the graph leg reads): for every digest
inserted, `summary:<previous session> --precedes--> summary:<this session>`
and, when a later digest already exists (out-of-order ingest),
`summary:<this session> --precedes--> summary:<next session>`. "Previous"
and "next" are by the digest's event time — `valid_at` (the observation
date) when set, else `created_at` — within the same (org, namespace,
agent, user_id). Keys are the `summary:<session_id>` keys the search leg
gives digest hits, so an edge found here names rows the reader can fetch.

Nothing here calls a model. Every write is fail-open: a chain edge that
cannot be written leaves the digest in place, and the digest write never
waits on the edge.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now

log = logging.getLogger(__name__)

DIGEST_KEY_PREFIX = "summary:"
CHAIN_RELATION = "precedes"
CHAIN_RUN_ID = "digest_chain"
CHAIN_AUTHOR = "loom"


def digest_key(session_id: str) -> str:
    """The key a digest carries in search results and in the edge table."""
    return f"{DIGEST_KEY_PREFIX}{session_id}"


def chain_edge_row(
    *,
    org: str,
    namespace: str,
    from_session: str,
    to_session: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One `loom.operator_edges` row: `from_session` precedes `to_session`.

    Every column the table declares is set (the table is
    ReplacingMergeTree(created_at) on (org, namespace, from, to, relation);
    an omitted column would land the DEFAULT). `author`/`run_id` mark the
    row as loom's own so an operator can tell it from an agent's
    `memory.link` and a backfill can find what it already wrote."""
    ts = now or naive_utc_now()
    return {
        "org": org,
        "namespace": namespace,
        "from_key": digest_key(from_session),
        "to_key": digest_key(to_session),
        "relation": CHAIN_RELATION,
        "label": "",
        "explanation": "consecutive session digests of one identity, by event time",
        "evidence": [from_session, to_session],
        "confidence": 1.0,
        "weight": 1.0,
        "author": CHAIN_AUTHOR,
        "run_id": CHAIN_RUN_ID,
        "created_at": ts,
        "tombstoned_at": None,
    }


_IDENTITY_WHERE = """
      org = {org:String}
      AND namespace = {ns:String}
      AND agent = {agent:String}
      AND user_id = {uid:String}
      AND tombstoned_at IS NULL
      AND session_id != {sid:String}
"""


async def neighbours(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    at: datetime,
) -> tuple[str, str]:
    """(previous session_id, next session_id) around `at` for one identity;
    '' where none exists. Two indexed reads on the digest table's ORDER BY
    prefix. Ties on event time break on session_id so two sessions dated
    the same day still form one chain (the later session_id is 'after')."""
    params = {
        "org": org,
        "ns": namespace,
        "agent": agent or "-",
        "uid": user_id or "-",
        "sid": session_id,
        "at": at,
    }
    prev_rows = await ch.query_all(
        f"""
        SELECT session_id
        FROM loom.session_summaries FINAL
        WHERE {_IDENTITY_WHERE}
          AND (coalesce(valid_at, created_at) < {{at:DateTime}}
               OR (coalesce(valid_at, created_at) = {{at:DateTime}}
                   AND session_id < {{sid:String}}))
        ORDER BY coalesce(valid_at, created_at) DESC, session_id DESC
        LIMIT 1
        """,
        params=params,
    )
    next_rows = await ch.query_all(
        f"""
        SELECT session_id
        FROM loom.session_summaries FINAL
        WHERE {_IDENTITY_WHERE}
          AND (coalesce(valid_at, created_at) > {{at:DateTime}}
               OR (coalesce(valid_at, created_at) = {{at:DateTime}}
                   AND session_id > {{sid:String}}))
        ORDER BY coalesce(valid_at, created_at) ASC, session_id ASC
        LIMIT 1
        """,
        params=params,
    )
    prev_sid = str((prev_rows or [{}])[0].get("session_id") or "") if prev_rows else ""
    next_sid = str((next_rows or [{}])[0].get("session_id") or "") if next_rows else ""
    return prev_sid, next_sid


async def link_digest(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    at: datetime,
) -> int:
    """Write the chain edges around one freshly inserted digest. Returns the
    number of edges written (0, 1 or 2). Fail-open: any error is logged and
    0 is returned — the digest itself is already in place."""
    try:
        prev_sid, next_sid = await neighbours(
            ch,
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            at=at,
        )
        rows: list[dict[str, Any]] = []
        if prev_sid:
            rows.append(
                chain_edge_row(
                    org=org, namespace=namespace, from_session=prev_sid, to_session=session_id
                )
            )
        if next_sid:
            rows.append(
                chain_edge_row(
                    org=org, namespace=namespace, from_session=session_id, to_session=next_sid
                )
            )
        if rows:
            await ch.insert("loom.operator_edges", rows)
        return len(rows)
    except Exception as e:  # noqa: BLE001 — additive; the digest is already written
        log.warning("digest_chain: edge write skipped for session=%r: %s", session_id, e)
        return 0
