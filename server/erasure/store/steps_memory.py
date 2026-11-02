"""Erasure steps for memory itself and the tables written beside it: the
memory, embedding, working-memory and shared rows, chunks, versions, derived
facts, reinforcement, procedure runs, session outcomes, term aggregates, raw
evidence, the events scrub and the code index of a code namespace."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from server.core.ch_params import chunks
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import (
    count_then_mutate,
    delete_scoped,
    namespaces_of,
    purge_mode,
    tombstone_scoped,
)
from server.erasure.store.predicates import CHUNK_KEYS_IN, NS_EQ, ORG_NS, ORG_UID, UID
from server.erasure.store.scope_reads import hashes, slices_or_one_empty, user_key_pairs
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.memory.domain.code_link_keys import IDENTITY, LINK_KEY


async def purge_code_index_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    code_tables: Callable[[_CHClient], Awaitable[list[str]]],
    refresh_views: Callable[[_CHClient], Awaitable[list[str]]],
    ch: _CHClient | None = None,
) -> int:
    """The repository index of a code namespace leaves with the namespace.
    Every `loom.code_*` table keyed by (org, namespace) — `code_tables` reads
    them from the live schema, so a newly added table is purged without a code
    change — through the saga's count-then-mutate: DELETE in every mode (the
    rows are the repository's index, not a subject's memory; there is no
    tombstone to keep), lightweight on the two wide content tables, a plain
    mutation on the rest, each awaited by its tag. A memory namespace pays
    one keyed count() per table and issues nothing. When rows went,
    `refresh_views` rebuilds the tables a refreshable view derives from them.
    Returns rows removed."""
    ch = ch or _default_client()
    total = 0
    for table in await code_tables(ch):
        total += await count_then_mutate(
            table,
            count_where=ORG_NS,
            mutation="DELETE",
            mutation_where=ORG_NS,
            params={"org": org, "ns": namespace},
            final=False,
            lightweight=True,
            ch=ch,
        )
    # the tables a refreshable view rebuilds from the ones purged above
    if total:
        await refresh_views(ch)
    return total


async def tombstone_memory_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.memory", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


async def tombstone_embeddings_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.memory_embeddings", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


async def tombstone_working_memory_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.working_memory", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


# The `args` entries a request row keeps that name the subject's own rows:
# the identifier-class keys of event_args.IDENTIFIER_KEYS that address a
# memory, a thread, a block or a handoff. The other identifier
# keys (`pattern`, `path`, `name`, `status`, …) are operator- or
# schema-authored names, not the subject's; the free-text keys are digests
# already. `user_id` / `session_id` never reach `args` (IDENTITY_KEYS).
ARGS_SCRUB_KEYS: tuple[str, ...] = (
    "memory_key",
    "memory_keys",
    "memory_id",
    "memory_ids",
    "target_key",
    "from_memory_key",
    "to_memory_key",
    "thread_id",
    "block_key",
    "handoff_id",
    "message_id",
    "record_id",
    "entity",
    "subject",
)
ERASED = "<erased>"
_ARGS_SCRUB_IN = ", ".join(f"'{k}'" for k in ARGS_SCRUB_KEYS)
# One UPDATE: the identity column, the memory_key column (kept '' when it was
# '' so "no key" stays distinguishable from "a scrubbed key"), and the
# identifier entries of the args map rewritten in place — the map keeps its
# keys so `args['_sha256']` and the op-mix classifier (`kind`, `scope`, …)
# still read; only the values that named the subject's rows are gone.
EVENTS_SCRUB_MUTATION = (
    f"UPDATE user_id = '{ERASED}', "
    f"memory_key = if(memory_key = '', '', '{ERASED}'), "
    f"args = mapApply((k, v) -> (k, if(k IN ({_ARGS_SCRUB_IN}), '{ERASED}', v)), args)"
)


async def scrub_events_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Replace user_id with '<erased>' on loom.agent_events, and the
    `memory_key` column and the identifier entries of `args` that named the
    subject's rows; on an end user's own calls, the `subject` that
    names them (`user:<user_id>`) too. Preserves aggregate cost rollups;
    removes the identity and what it pointed at."""
    where = "org = {org:String} AND (user_id = {uid:String} OR subject = {p:String})"
    return await count_then_mutate(
        "loom.agent_events",
        count_where=where,
        mutation=EVENTS_SCRUB_MUTATION
        + f", subject = if(subject = {{p:String}}, '{ERASED}', subject)",
        mutation_where=where,
        params={"org": org, "uid": user_id, "p": "user:" + user_id},
        final=False,
        ch=ch,
    )


async def delete_last_seen_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """The newest-call row of an end user (`user:<user_id>`). Its
    refresh reads only ledger rows newer than its mark, which the scrub has
    already taken the subject off, so the row is not written back."""
    where = "org = {org:String} AND subject = {p:String}"
    return await count_then_mutate(
        "loom.subject_last_seen",
        count_where=where,
        mutation="DELETE",
        mutation_where=where,
        params={"org": org, "p": "user:" + user_id},
        final=False,
        ch=ch,
    )


# A shared row promoted from one of the user's memories: an entry of
# `promoted_from` (`org/namespace/agent/user_id/session_id/memory_key`) whose
# fourth segment is the user.
_PROMOTED_FROM_USER = "arrayExists(x -> splitByChar('/', x)[4] = {uid:String}, promoted_from)"


async def tombstone_shared_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Tombstone loom.shared rows whose `promoted_from` includes a
    source-tuple repr matching this user_id.

    The repr format is `org/namespace/agent/user_id/session_id/memory_key`,
    so position 4 of the slash-split is the user_id. If ANY entry names the
    erased user, the shared row is tombstoned — partial-source erasure still
    requires full removal because the value may carry that user's contribution.
    """
    return await tombstone_scoped(
        "loom.shared", org=org, scope_where=_PROMOTED_FROM_USER, params={"uid": user_id}, ch=ch
    )


async def tombstone_shared_embeddings_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Tombstone the scope='shared' embedding rows that back any shared
    memory whose source includes this user_id.

    Shared embeddings don't directly carry user_id (sentinel
    agent='_shared'), so the keys of the loom.shared rows the same
    promoted_from predicate matches are read first, then named.
    """
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT DISTINCT memory_key FROM loom.shared FINAL "
        f"WHERE org = {{org:String}} AND {_PROMOTED_FROM_USER}",
        params={"org": org, "uid": user_id},
    )
    total = 0
    for part in chunks(sorted(str(r["memory_key"]) for r in rows)):
        total += await tombstone_scoped(
            "loom.memory_embeddings",
            org=org,
            scope_where="scope = 'shared' AND memory_key IN {keys:Array(String)}",
            params={"keys": part},
            ch=ch,
        )
    return total


async def tombstone_memory_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.memory", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def tombstone_embeddings_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.memory_embeddings", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def tombstone_working_memory_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.working_memory", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def tombstone_entity_canonical_map_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    return await tombstone_scoped(
        "loom.entity_canonical_map", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


# The rows erasure's mutation stamps; liveness itself is LIVE_CHUNK_SQL.
_UNSUPERSEDED = "doc_superseded_at IS NULL"


async def supersede_chunks_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    # A purge deletes every chunk of the scope, superseded ones included: the
    # supersede below keeps the text in the parts, and the purge census counts
    # every row.
    if purge_mode():
        return await count_then_mutate(
            "loom.chunks",
            count_where=ORG_NS,
            mutation="DELETE",
            mutation_where=ORG_NS,
            params={"org": org, "ns": namespace},
            final=False,
            ch=ch,
        )
    return await count_then_mutate(
        "loom.chunks",
        # Live = no `doc_superseded_at` and no supersession row.
        count_where="org = {org:String} AND namespace = {ns:String} AND " + LIVE_CHUNK_SQL,
        mutation="UPDATE doc_superseded_at = {now:DateTime}",
        mutation_where=("org = {org:String} AND namespace = {ns:String} AND " + _UNSUPERSEDED),
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


async def supersede_chunks_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """The chunks of every document the user wrote, in any namespace of the
    org: superseded, or deleted, every version, under a purge."""
    if purge_mode():
        return await count_then_mutate(
            "loom.chunks",
            count_where=ORG_UID,
            mutation="DELETE",
            mutation_where=ORG_UID,
            params={"org": org, "uid": user_id},
            final=False,
            ch=ch,
        )
    return await count_then_mutate(
        "loom.chunks",
        count_where=ORG_UID + " AND " + LIVE_CHUNK_SQL,
        mutation="UPDATE doc_superseded_at = {now:DateTime}",
        mutation_where=ORG_UID + " AND " + _UNSUPERSEDED,
        params={"org": org, "uid": user_id},
        final=False,
        ch=ch,
    )


async def delete_derived_facts_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete per-occurrence derived facts for a namespace. derived_facts
    is plain MergeTree with no soft-delete column, so erasure is an ALTER
    DELETE, not a tombstone."""
    return await count_then_mutate(
        "loom.derived_facts",
        count_where="org = {org:String} AND namespace = {ns:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


async def delete_memory_reinforcement_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete the namespace's folded reinforcement rows.
    ReplacingMergeTree keyed on identity + memory_key with no soft-delete
    column, so erasure is an ALTER DELETE."""
    return await count_then_mutate(
        "loom.memory_reinforcement",
        count_where="org = {org:String} AND namespace = {ns:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


async def delete_procedure_runs_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's procedure run ledger (loom.procedure_runs:
    agent, session_id, visited node ids and the agent's free-text note per
    run). Plain MergeTree, no soft-delete column → ALTER DELETE."""
    return await count_then_mutate(
        "loom.procedure_runs",
        count_where="org = {org:String} AND namespace = {ns:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


async def delete_query_nodes_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's query nodes (loom.query_nodes: the text of
    every question asked, its embedding and what it returned).
    AggregatingMergeTree, no soft-delete column → ALTER DELETE."""
    return await count_then_mutate(
        "loom.query_nodes",
        count_where="org = {org:String} AND namespace = {ns:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


SESSION_OUTCOME_TABLES = ("loom.session_outcomes_by_user",)


async def delete_session_outcomes_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's session outcome labels (a judge's one-line
    reading of each session). RMT with no soft-delete column → ALTER DELETE."""
    (table,) = SESSION_OUTCOME_TABLES
    return await count_then_mutate(
        table,
        count_where="org = {org:String} AND namespace = {ns:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


async def delete_session_outcomes_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Hard-delete one user's session outcome labels (the judge's reason
    reads their session). A row carrying user '-' leaves with its namespace."""
    return await delete_scoped(
        SESSION_OUTCOME_TABLES[0], org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


# Derived-text aggregates: every distinct term of every memory excerpt and
# knowledge chunk in a namespace, plus per-row lengths. The words ARE the
# content, so they sit inside the erasure boundary. Plain AggregatingMergeTree
# / ReplacingMergeTree with no soft-delete column → ALTER DELETE. No user_id
# on any of them: namespace scope only.
TERM_AGGREGATE_TABLES: tuple[str, ...] = (
    "loom.memory_term_freq",
    "loom.memory_doc_stats",
    "loom.chunk_term_freq",
)


async def delete_term_aggregates_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's rows from every term-frequency / doc-stats
    aggregate. Returns the total rows deleted across the tables."""
    total = 0
    for table in TERM_AGGREGATE_TABLES:
        total += await count_then_mutate(
            table,
            count_where="org = {org:String} AND namespace = {ns:String}",
            mutation="DELETE",
            mutation_where="org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": namespace},
            final=False,
            ch=ch,
        )
    return total


# Raw transcripts outlive their memories unless erased explicitly: every
# memory.set_from_messages call persists the caller's messages verbatim in
# loom.memory_evidence (one row per message, with user_id), and a deferred
# call parks the whole request JSON in loom.memory_extraction_queue until the
# extraction_queue worker replays it. Neither table carries tombstoned_at;
# both are ReplacingMergeTree → ALTER DELETE.
_RAW_EVIDENCE_TABLES = ("loom.memory_evidence", "loom.memory_extraction_queue")


async def delete_raw_evidence_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a user's raw transcripts (evidence rows + queued
    extraction requests) across namespaces. Returns rows deleted."""
    total = 0
    for table in _RAW_EVIDENCE_TABLES:
        total += await count_then_mutate(
            table,
            count_where="org = {org:String} AND user_id = {uid:String}",
            mutation="DELETE",
            mutation_where="org = {org:String} AND user_id = {uid:String}",
            params={"org": org, "uid": user_id},
            ch=ch,
        )
    return total


async def delete_raw_evidence_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's raw transcripts (evidence rows + queued
    extraction requests). Returns rows deleted."""
    total = 0
    for table in _RAW_EVIDENCE_TABLES:
        total += await count_then_mutate(
            table,
            count_where="org = {org:String} AND namespace = {ns:String}",
            mutation="DELETE",
            mutation_where="org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": namespace},
            ch=ch,
        )
    return total


async def tombstone_memory_chunks_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Tombstone the verbatim chunks of a user: the chunks stamped with the
    user, and the chunks whose parent memories' (namespace, key) pairs are the
    user's, read from loom.memory (tombstoned rows included — the memory step
    runs first and the chunks must follow it, not escape it)."""
    ch = ch or _default_client()
    total = 0
    for part in slices_or_one_empty(await user_key_pairs(org, user_id, ch=ch)):
        total += await tombstone_scoped(
            "loom.memory_chunks",
            org=org,
            scope_where=CHUNK_KEYS_IN,
            params={"uid": user_id, "keys": part},
            ch=ch,
        )
    return total


async def tombstone_memory_chunks_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Tombstone every verbatim memory chunk in a namespace."""
    return await tombstone_scoped(
        "loom.memory_chunks", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_memory_versions_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's version history (loom.memory_versions).
    The history exists so a rollback can re-insert an old
    head; after erasure that would resurrect erased content, so the rows go,
    not a tombstone."""
    return await count_then_mutate(
        "loom.memory_versions",
        count_where="org = {org:String} AND namespace = {ns:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
        final=False,
        ch=ch,
    )


async def delete_memory_versions_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a user's version history across namespaces (see the
    namespace variant)."""
    return await count_then_mutate(
        "loom.memory_versions",
        count_where="org = {org:String} AND user_id = {uid:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND user_id = {uid:String}",
        params={"org": org, "uid": user_id},
        final=False,
        ch=ch,
    )


# A memory's links to code, and the queue of memory writes the link worker reads.
_CODE_LINK_TABLES = ("loom.memory_code_links", "loom.memory_code_link_queue")


_CURSORS = "loom.code_index_state"
_LINK_CURSOR_NS = "memory_code_links"  # the link worker's cursors (`code_link_pass.Cursor`)
_LINK_CURSOR_KEYS = ("queue", "backfill", "sweep")
_NO_MEMORY = "00000000-0000-0000-0000-000000000000"
# A queue position is (queued_at, version, *IDENTITY); backfill's `after` is an IDENTITY,
# sweep's a link key.
_QUEUED = 2
_ORG, _NS, _UID = (IDENTITY.index(c) for c in ("org", "namespace", "user_id"))
_LINK_ORG, _LINK_NS = (LINK_KEY.index(c) for c in ("org", "namespace"))


def _scrubbed(key: str, value: str, org: str, namespaces: set[str], user_id: str) -> str | None:
    """The link worker's cursor `key` without the erased memories' identity, None when it
    names none. The cursor stays where it was: the identity it drops only re-reads a
    few rows."""
    data = json.loads(value) if value else None

    def names(who: Sequence[str]) -> bool:
        return who[_ORG] == org and (
            who[_NS] in namespaces or bool(user_id) and who[_UID] == user_id
        )

    if key == "queue" and isinstance(data, list) and data and names(data[_QUEUED:]):
        return json.dumps([*data[:_QUEUED], *[""] * len(IDENTITY)])
    if not isinstance(data, dict) or not (after := data.get("after")):
        return None
    if key == "backfill" and names(after):
        kept = [
            v if c in ("org", "namespace") else "" for c, v in zip(IDENTITY, after, strict=True)
        ]
        return json.dumps({**data, "after": kept})
    if key == "sweep" and after[_LINK_ORG] == org and (after[_LINK_NS] in namespaces or user_id):
        kept = [_NO_MEMORY if c == "memory_id" else v for c, v in zip(LINK_KEY, after, strict=True)]
        return json.dumps({**data, "after": kept})
    return None


async def _scrub_link_cursors(
    org: str, namespaces: set[str], user_id: str, ch: _CHClient | None
) -> None:
    """Rewrite the link worker's cursors that name an erased memory, and delete every
    older version of them: one not yet merged away may name it while the current one
    does not."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT state_key, state_value, version FROM {_CURSORS} FINAL "
        "WHERE org = '' AND namespace = {ns:String} AND state_key IN {keys:Array(String)}",
        params={"ns": _LINK_CURSOR_NS, "keys": list(_LINK_CURSOR_KEYS)},
    )
    for r in rows:
        version = int(r["version"])
        value = _scrubbed(str(r["state_key"]), str(r["state_value"]), org, namespaces, user_id)
        if value is not None:
            version += 1
            await ch.insert(
                _CURSORS,
                [
                    {
                        "org": "",
                        "namespace": _LINK_CURSOR_NS,
                        "state_key": r["state_key"],
                        "state_value": value,
                        "updated_at": naive_utc_now(),
                        "version": version,
                    }
                ],
            )
        await ch.execute(
            f"DELETE FROM {_CURSORS} WHERE org = '' AND namespace = {{ns:String}} "
            "AND state_key = {key:String} AND version < {v:UInt64} "
            "SETTINGS lightweight_deletes_sync = 2",
            params={"ns": _LINK_CURSOR_NS, "key": r["state_key"], "v": version},
        )


async def delete_code_links_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a namespace's memory-to-code links and queued writes: they carry the
    memories' keys."""
    n = 0
    for table in _CODE_LINK_TABLES:
        n += await count_then_mutate(
            table,
            count_where="org = {org:String} AND namespace = {ns:String}",
            mutation="DELETE",
            mutation_where="org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": namespace},
            final=False,
            ch=ch,
        )
    names = {namespace} if isinstance(namespace, str) else set(namespace)
    await _scrub_link_cursors(org, names, "", ch)
    return n


async def delete_code_links_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a user's memory-to-code links and queued writes across namespaces."""
    n = 0
    for table in _CODE_LINK_TABLES:
        n += await count_then_mutate(
            table,
            count_where="org = {org:String} AND user_id = {uid:String}",
            mutation="DELETE",
            mutation_where="org = {org:String} AND user_id = {uid:String}",
            params={"org": org, "uid": user_id},
            final=False,
            ch=ch,
        )
    await _scrub_link_cursors(org, set(), user_id, ch)
    return n


async def delete_derived_facts_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a user's per-occurrence derived facts across namespaces."""
    return await count_then_mutate(
        "loom.derived_facts",
        count_where="org = {org:String} AND user_id = {uid:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND user_id = {uid:String}",
        params={"org": org, "uid": user_id},
        final=False,
        ch=ch,
    )


async def delete_memory_reinforcement_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a user's folded reinforcement rows across namespaces."""
    return await count_then_mutate(
        "loom.memory_reinforcement",
        count_where="org = {org:String} AND user_id = {uid:String}",
        mutation="DELETE",
        mutation_where="org = {org:String} AND user_id = {uid:String}",
        params={"org": org, "uid": user_id},
        final=False,
        ch=ch,
    )


async def tombstone_documents_by_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Supersede the namespace's whole-document rows through the read-filtered
    `superseded_at` column; a purge deletes them instead. The `IS NULL` guard
    makes a re-run a no-op."""
    if not purge_mode():
        return await _supersede_documents(ORG_NS + " AND superseded_at IS NULL", org, namespace, ch)
    return await count_then_mutate(
        "loom.documents",
        count_where=ORG_NS,
        mutation="DELETE",
        mutation_where=ORG_NS,
        params={"org": org, "ns": namespace},
        ch=ch,
    )


# Another writer's live versions of a document whose newest version is the user's:
# what deleting the user's rows would otherwise bring back. `docs` holds the
# documents read before the user's rows are deleted, each as
# `cityHash64(namespace, source_uri)`: nothing bounds a stored URI.
_USERS_DOCUMENTS = (
    "SELECT DISTINCT cityHash64(namespace, source_uri) AS h FROM loom.documents FINAL"
    " WHERE org = {org:String} AND user_id = {uid:String}"
)
_REPLACED_BY_USER = (
    "org = {org:String} AND user_id != {uid:String} AND superseded_at IS NULL"
    " AND cityHash64(namespace, source_uri) IN {docs:Array(UInt64)}"
)


async def tombstone_documents_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Supersede the documents the user wrote, in any namespace of the org; a
    purge deletes every version of theirs instead, after superseding another
    writer's versions that one of theirs replaced, so none reads as live again."""
    ch = ch or _default_client()
    if purge_mode():
        rows = await ch.query_all(_USERS_DOCUMENTS, params={"org": org, "uid": user_id})
        for part in chunks(hashes(rows)):
            await count_then_mutate(
                "loom.documents",
                count_where=_REPLACED_BY_USER,
                mutation="UPDATE superseded_at = {now:DateTime}",
                mutation_where=_REPLACED_BY_USER,
                params={"org": org, "uid": user_id, "docs": part},
                final=False,  # those versions are hidden under FINAL until the delete
                ch=ch,
            )
        return await count_then_mutate(
            "loom.documents",
            count_where=ORG_UID,
            mutation="DELETE",
            mutation_where=ORG_UID,
            params={"org": org, "uid": user_id},
            final=False,  # every version the user wrote, the replaced ones too
            ch=ch,
        )
    where = ORG_UID + " AND superseded_at IS NULL"
    return await count_then_mutate(
        "loom.documents",
        count_where=where,
        mutation="UPDATE superseded_at = {now:DateTime}",
        mutation_where=where,
        params={"org": org, "uid": user_id},
        ch=ch,
    )


# The originals the user uploaded (`loom.blob_uploads`: a document version
# naming one may since have been replaced and merged away), or that a version
# of their documents still names, that no other writer's live document shares
# (two documents with the same bytes share one row). Read, then deleted by name.
_USERS_OWN_BLOBS = (
    "org = {org:String} AND (namespace, toString(sha256)) IN ("
    " SELECT namespace, toString(sha256) FROM loom.blob_uploads"
    " WHERE org = {org:String} AND user_id = {uid:String}"
    " UNION ALL SELECT namespace, blob_path FROM loom.documents"
    " WHERE org = {org:String} AND user_id = {uid:String} AND blob_path != '')"
    " AND (namespace, toString(sha256)) NOT IN ("
    " SELECT namespace, blob_path FROM loom.documents FINAL"
    " WHERE org = {org:String} AND user_id != {uid:String} AND blob_path != ''"
    " AND superseded_at IS NULL)"
)
_USERS_UNERASED = ORG_UID + " AND blob_path != '' AND blob_erased_at IS NULL"
_BLOBS_IN = "(namespace, toString(sha256)) IN {blobs:Array(Tuple(String, String))}"


async def _users_own_blobs(
    org: str, user_id: str, ch: _CHClient, among: list[tuple[str, str]] | None = None
) -> list[tuple[str, str]]:
    """The (namespace, sha256) of the originals `_USERS_OWN_BLOBS` names, of `among`
    when given."""
    where = _USERS_OWN_BLOBS
    params: dict[str, Any] = {"org": org, "uid": user_id}
    if among is not None:
        where += f" AND {_BLOBS_IN}"
        params["blobs"] = among
    rows = await ch.query_all(
        "SELECT DISTINCT namespace, toString(sha256) AS sha "
        f"FROM loom.document_blobs WHERE {where}",
        params=params,
    )
    return sorted({(str(r["namespace"]), str(r["sha"])) for r in rows})


async def erase_blobs_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Delete the user's uploaded originals no other writer shares, stamp
    `blob_erased_at` on every document of the user's that had one, then delete
    the record of their uploads. Returns the originals deleted.

    Each slice of originals is read again right before its DELETE, so an original that
    another writer's document named since the first read stays."""
    ch = ch or _default_client()
    n = 0
    for part in chunks(await _users_own_blobs(org, user_id, ch)):
        still_own = await _users_own_blobs(org, user_id, ch, part)
        if still_own:
            n += await delete_scoped(
                "loom.document_blobs",
                org=org,
                scope_where=_BLOBS_IN,
                params={"blobs": still_own},
                ch=ch,
            )
    await count_then_mutate(
        "loom.documents",
        count_where=_USERS_UNERASED,
        mutation="UPDATE blob_erased_at = {now:DateTime}",
        mutation_where=_USERS_UNERASED,
        params={"org": org, "uid": user_id},
        ch=ch,
    )
    await count_then_mutate(
        "loom.blob_uploads",
        count_where=ORG_UID,
        mutation="DELETE",
        mutation_where=ORG_UID,
        params={"org": org, "uid": user_id},
        final=False,
        ch=ch,
    )
    return n


async def _supersede_documents(
    where: str, org: str, namespace: str | Sequence[str], ch: _CHClient | None
) -> int:
    return await count_then_mutate(
        "loom.documents",
        count_where=where,
        mutation="UPDATE superseded_at = {now:DateTime}",
        mutation_where=where,
        params={"org": org, "ns": namespace},
        ch=ch,
    )


# The documents of the scope whose original has not been erased yet. One
# predicate for the stamp; a list scope binds as an Array, as
# `count_then_mutate` rewrites it — a list bound to `{ns:String}` is the
# literal "['a', 'b']" and matches nothing.
_UNERASED_BLOBS = (
    "org = {org:String} AND namespace IN {ns:Array(String)}"
    " AND blob_path != '' AND blob_erased_at IS NULL"
)


async def delete_document_blobs_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Every uploaded original stored for the scope (loom.document_blobs)."""
    return await delete_scoped(
        "loom.document_blobs", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_blob_uploads_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The record of who uploaded the scope's originals (loom.blob_uploads)."""
    return await delete_scoped(
        "loom.blob_uploads", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def stamp_blobs_erased(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Stamp `blob_erased_at` on the scope's documents whose originals were
    deleted, when any is unstamped: counted, then a mutation tagged and awaited
    like every other saga statement, so a resumed step waits for its own
    pending stamp."""
    return await count_then_mutate(
        "loom.documents",
        count_where=_UNERASED_BLOBS,
        mutation="UPDATE blob_erased_at = {now:DateTime}",
        mutation_where=_UNERASED_BLOBS,
        params={"org": org, "ns": namespaces_of(namespace)},
        ch=ch,
    )
