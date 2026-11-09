"""Erasure steps for memory itself and the tables written beside it: the
memory, embedding, working-memory and shared rows, chunks, versions, derived
facts, reinforcement, procedure runs, session outcomes, term aggregates, raw
evidence, the events scrub and the code index of a code namespace."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from server.core import object_store
from server.core.background import gather_or_cancel
from server.core.ch import sequential_reads
from server.core.ch_params import chunks
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import (
    count_then_mutate,
    delete_scoped,
    namespaces_of,
    purge_mode,
    purge_scope,
    tombstone_scoped,
)
from server.erasure.store.predicates import (
    CHUNK_KEYS_IN,
    CONSOLIDATED_IN,
    NS_EQ,
    NS_IN,
    ORG_NS,
    ORG_UID,
    PARENT_KEYS_IN,
    SUMMARY_CHUNKS_IN,
    UID,
)
from server.erasure.store.scope_reads import (
    consolidated_slices,
    hashes,
    slices_or_one_empty,
    user_key_pairs,
    user_summary_rows,
)
from server.knowledge.domain.ingest_states import OPEN_STATES
from server.memory.domain.code_link_keys import IDENTITY, LINK_KEY
from server.settings import settings


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
    """A tombstone even in a purge: the steps after this one find what the user's
    memories fed by their keys, read from these rows. `memory.retire` purges them
    after those steps, and counts them then."""
    purge = purge_mode()
    with purge_scope(False):
        n = await tombstone_scoped(
            "loom.memory", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
        )
    return 0 if purge else n


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
# Or from a summary of the user's memories, by the whole entry: a key or a session may
# hold slashes.
_PROMOTED_FROM_SUMMARY = "hasAny(promoted_from, {sources:Array(String)})"
_PROMOTED_FROM = f"({_PROMOTED_FROM_USER} OR {_PROMOTED_FROM_SUMMARY})"


async def _promoted_slices(org: str, user_id: str, *, ch: _CHClient) -> list[dict[str, Any]]:
    """The bound values of `_PROMOTED_FROM`, a slice of the summaries at a time."""
    summaries = (await user_summary_rows(org, user_id, ch=ch)).sources
    return [{"uid": user_id, "sources": part} for part in slices_or_one_empty(summaries)]


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
    requires full removal because the value may carry that user's contribution. So is
    one promoted from a summary of the user's memories.
    """
    ch = ch or _default_client()
    total = 0
    for params in await _promoted_slices(org, user_id, ch=ch):
        total += await tombstone_scoped(
            "loom.shared", org=org, scope_where=_PROMOTED_FROM, params=params, ch=ch
        )
    return total


async def tombstone_shared_embeddings_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Tombstone the scope='shared' embedding rows that back any shared
    memory whose source includes this user_id, or a summary of the user's memories.

    Shared embeddings don't directly carry user_id (sentinel
    agent='_shared'), so the (namespace, key) pairs of the loom.shared rows
    the same promoted_from predicate matches are read first, then named. The
    key alone would reach another namespace's shared row of the same spelling.
    """
    ch = ch or _default_client()
    found: set[tuple[str, str]] = set()
    for params in await _promoted_slices(org, user_id, ch=ch):
        rows = await ch.query_all(
            "SELECT DISTINCT namespace, memory_key FROM loom.shared FINAL "
            f"WHERE org = {{org:String}} AND {_PROMOTED_FROM}",
            params={"org": org, **params},
        )
        found.update((str(r["namespace"]), str(r["memory_key"])) for r in rows)
    pairs = sorted(found)
    total = 0
    for part in chunks(pairs):
        total += await tombstone_scoped(
            "loom.memory_embeddings",
            org=org,
            scope_where=f"scope = 'shared' AND {PARENT_KEYS_IN}",
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


# The chunks an erasure stamps: every one not stamped yet, a retired one too, since the
# stamp is what tells knowledge.restore an erasure reached the rows a delete retired.
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
        count_where=ORG_NS + " AND " + _UNSUPERSEDED,
        mutation="UPDATE doc_superseded_at = {now:DateTime}",
        mutation_where=ORG_NS + " AND " + _UNSUPERSEDED,
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
        count_where=ORG_UID + " AND " + _UNSUPERSEDED,
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
    user, and the no-user ('-') chunks whose parent (namespace, key) pairs are
    the user's, read from loom.memory (tombstoned rows included — the memory
    step runs first and the chunks must follow it, not escape it). Another
    user's chunks of a shared key stay (`CHUNK_KEYS_IN`). The chunks of a summary of
    the user's memories go too, by the summary's agent, user and key."""
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
    identities = (await user_summary_rows(org, user_id, ch=ch)).identities
    # a legacy '-' chunk of the summary too, as the write path retires it as its own
    legacy = {(ns, agent, "-", key) for ns, agent, _, key in identities}
    for rows in chunks(sorted(set(identities) | legacy)):
        total += await tombstone_scoped(
            "loom.memory_chunks",
            org=org,
            scope_where=SUMMARY_CHUNKS_IN,
            params={"rows": rows},
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
    return await delete_scoped(
        "loom.memory_versions", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_memory_versions_by_user(
    org: str,
    user_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete a user's version history across namespaces (see the
    namespace variant), and that of the summaries built from the user's memories,
    which `memory.history` reads and a rollback re-inserts. After the cache steps:
    they find an answer that quoted a summary by its history."""
    ch = ch or _default_client()
    total = await delete_scoped(
        "loom.memory_versions", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )
    for params in await consolidated_slices(org, user_id, ch=ch):
        total += await delete_scoped(
            "loom.memory_versions", org=org, scope_where=CONSOLIDATED_IN, params=params, ch=ch
        )
    return total


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


# The (namespace, sha256) of every original the user wrote that no other writer's
# live document shares (two documents with the same bytes share one row), read
# from the record of their uploads (`loom.blob_uploads`: a document version that
# named one may since have been replaced and merged away) and from their
# documents, originals and extraction artifacts alike, so bytes whose catalog
# row never landed are found too.
_USERS_OWN_ORIGINALS = (
    "SELECT DISTINCT namespace, sha FROM ("
    " SELECT namespace, toString(sha256) AS sha FROM loom.blob_uploads"
    " WHERE org = {org:String} AND user_id = {uid:String}"
    " UNION ALL SELECT namespace, blob_path AS sha FROM loom.documents"
    " WHERE org = {org:String} AND user_id = {uid:String} AND blob_path != ''"
    " UNION ALL SELECT namespace, artifact_sha256 AS sha FROM loom.documents"
    " WHERE org = {org:String} AND user_id = {uid:String} AND artifact_sha256 != '')"
    # Not one another writer still needs: the original or artifact of their live
    # document, an original their unexpired upload or open ingest job holds.
    " WHERE (namespace, sha) NOT IN ("
    " SELECT namespace, blob_path FROM loom.documents FINAL"
    " WHERE org = {org:String} AND user_id != {uid:String} AND blob_path != ''"
    " AND superseded_at IS NULL"
    " UNION ALL SELECT namespace, artifact_sha256 FROM loom.documents FINAL"
    " WHERE org = {org:String} AND user_id != {uid:String} AND artifact_sha256 != ''"
    " AND superseded_at IS NULL"
    " UNION ALL SELECT namespace, toString(sha256) FROM loom.knowledge_uploads"
    " WHERE org = {org:String} AND user_id != {uid:String} AND expires_at > now64(3)"
    " UNION ALL SELECT namespace, toString(sha256) FROM loom.knowledge_ingestions FINAL"
    " WHERE org = {org:String} AND user_id != {uid:String}"
    " AND state IN ('pending', 'processing'))"
)
# The catalog rows of those originals, by the pairs bound: reading the subquery
# again could name rows whose objects the step did not delete.
_USERS_OWN_BLOBS = (
    "org = {org:String} AND (namespace, toString(sha256)) IN {pairs:Array(Tuple(String, String))}"
)
_USERS_UNERASED = ORG_UID + " AND blob_path != '' AND blob_erased_at IS NULL"


async def _erasure_bucket(scope_where: str, params: dict[str, object], ch: _CHClient) -> str:
    """The bucket the scope's objects are deleted from ('' for none configured).
    The step fails closed when a row of the scope names an object it cannot
    reach: no bucket is configured, or the row's object lies outside the
    configured bucket and prefix (written under a former setting)."""
    bucket = settings.document_s3_bucket
    root = object_store.Location(bucket, object_store.root_prefix()).uri()
    row = await ch.query_one(
        "SELECT countIf(location != '') AS stored,"
        # Four segments below the root, as Loom lays a key out; not matched to the scope.
        " countIf(location != '' AND NOT (startsWith(location, {root:String}) AND"
        " length(splitByChar('/', substring(location, length({root:String}) + 1))) = 4))"
        " AS elsewhere"
        f" FROM loom.document_blobs WHERE {scope_where}",
        params={**params, "root": root},
    )
    stored = int((row or {}).get("stored") or 0)
    elsewhere = int((row or {}).get("elsewhere") or 0)
    if stored and not bucket:
        raise RuntimeError(
            "originals of this scope are in an object store and LOOM_DOCUMENT_S3_BUCKET is unset"
        )
    if elsewhere:
        raise RuntimeError(
            f"{elsewhere} original(s) of this scope are outside LOOM_DOCUMENT_S3_BUCKET and"
            " LOOM_DOCUMENT_S3_PREFIX"
        )
    return bucket


async def _listed(
    org: str, pairs: list[tuple[str, str]], bucket: str
) -> tuple[list[str], list[list[str]]]:
    """Each original's prefix of `pairs` and the keys listed under it; a prefix holds
    a write or two, and the listings share the store's listing threads."""
    prefixes = [object_store.original_prefix(org, ns, sha) for ns, sha in pairs]
    found = await gather_or_cancel(*(object_store.keys_under(bucket, p) for p in prefixes))
    return prefixes, found


async def _erase_user_objects(org: str, pairs: list[tuple[str, str]], bucket: str) -> None:
    """Delete every object holding bytes of the originals `pairs` names (each
    write of them, named by a row or not), and check the store kept none."""
    prefixes, found = await _listed(org, pairs, bucket)
    await object_store.delete(bucket, [k for listed in found for k in listed])
    # A prefix that listed nothing has nothing to recount.
    left = sum(
        await gather_or_cancel(
            *(
                object_store.count_under(bucket, p)
                for p, listed in zip(prefixes, found, strict=True)
                if listed
            )
        )
    )
    if left:
        raise object_store.ObjectStoreError(
            f"the object store still holds {left} erased object(s)", operation="erase"
        )


async def _clear_unnamed_user_objects(
    org: str, pairs: list[tuple[str, str]], bucket: str, ch: _CHClient
) -> None:
    """After the batch's rows are gone: delete the objects under `pairs`' prefixes
    that no catalog row names. A write whose row the mutation took is one; a write
    whose row is in by the read below keeps its object. A write of those bytes still in
    flight, by anyone (object stored, row not yet in), loses its object, and its row,
    when it lands, names a deleted one: its download answers 410."""
    _, found = await _listed(org, pairs, bucket)
    keys = [k for listed in found for k in listed]
    if not keys:
        return
    rows = await ch.query_all(
        "SELECT DISTINCT location FROM loom.document_blobs"
        f" WHERE {_USERS_OWN_BLOBS} AND location != ''",
        params={"org": org, "pairs": pairs},
    )
    named = {str(r["location"]) for r in rows}
    unnamed = [k for k in keys if object_store.Location(bucket, k).uri() not in named]
    await object_store.delete(bucket, unnamed)


async def erase_blobs_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Delete the user's uploaded originals no other writer shares (their
    objects first, then the rows naming them), stamp `blob_erased_at` on every
    document of the user's that had one, then delete the record of their
    uploads. Returns the originals deleted.

    The originals are the user's while no other writer's live document names
    the same bytes; each batch is read again right before its delete. Another
    writer storing the same bytes in the same namespace between that read and the
    delete can lose them, as with the ClickHouse store, where the bytes are the
    row the step deletes.

    Every read runs with sequential consistency: the pairs decide what is
    deleted and the counts decide whether a delete is sent, while the upload
    records go by user id at the end, so a replica behind a fresh upload would
    leave its bytes with nothing left to find them by."""
    ch = ch or _default_client()
    with sequential_reads():
        n = await _erase_user_originals_by_batch(org, user_id, ch)
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


async def _still_own(
    org: str, user_id: str, pairs: list[tuple[str, str]], ch: _CHClient
) -> list[tuple[str, str]]:
    """The originals of `pairs` that are still the user's alone."""
    rows = await ch.query_all(
        f"SELECT namespace, sha FROM ({_USERS_OWN_ORIGINALS})"
        " WHERE (namespace, sha) IN {pairs:Array(Tuple(String, String))}",
        params={"org": org, "uid": user_id, "pairs": pairs},
    )
    return sorted((str(r["namespace"]), str(r["sha"])) for r in rows)


async def _erase_user_originals_by_batch(org: str, user_id: str, ch: _CHClient) -> int:
    """The user's originals, read, then a batch at a time, each batch read again right
    before it is deleted, so an original another writer's document named since stays;
    with a bucket, its objects go before and after its rows."""
    rows = await ch.query_all(_USERS_OWN_ORIGINALS, params={"org": org, "uid": user_id})
    pairs = sorted((str(r["namespace"]), str(r["sha"])) for r in rows)
    n = 0
    # As many originals per statement as one bound parameter carries.
    for part in chunks(pairs):
        batch = await _still_own(org, user_id, part, ch)
        if not batch:
            continue
        batch_params: dict[str, object] = {"org": org, "pairs": batch}
        bucket = await _erasure_bucket(_USERS_OWN_BLOBS, batch_params, ch)
        if bucket:
            await _erase_user_objects(org, batch, bucket)
        # A mutation: a row from before a store switch holds its bytes in `data`.
        n += await count_then_mutate(
            "loom.document_blobs",
            count_where=_USERS_OWN_BLOBS,
            mutation="DELETE",
            mutation_where=_USERS_OWN_BLOBS,
            params=batch_params,
            final=False,
            ch=ch,
        )
        if bucket:
            await _clear_unnamed_user_objects(org, batch, bucket, ch)
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
    """Every uploaded original stored for the scope: every object of the scope
    under its prefixes, then the loom.document_blobs rows."""
    ch = ch or _default_client()
    names = namespaces_of(namespace)
    scope = "org = {org:String} AND " + NS_IN
    # Sequential: a replica behind a row naming an object outside the bucket
    # would let the step pass instead of failing closed.
    with sequential_reads():
        bucket = await _erasure_bucket(scope, {"org": org, "ns": names}, ch)
    if bucket:
        own = set(names)
        await object_store.delete_under(
            bucket,
            [object_store.namespace_prefix(org, n) for n in names],
            owned=lambda key: object_store.in_namespaces(key, org, own),
        )
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


# The ingestion tables (`loom.knowledge_uploads`, `loom.knowledge_ingestions`,
# `loom.knowledge_ingest_cancels`): a file name, a title, a description and
# metadata, and who asked, so they go with the documents of the scope; and the
# datasets an ingest typed from the file's tables (`loom.datasets`,
# `loom.dataset_rows`), the file's cell values.
_INGESTION_TABLES = (
    "loom.knowledge_uploads",
    "loom.knowledge_ingestions",
    "loom.knowledge_ingest_cancels",
    "loom.datasets",
    "loom.dataset_rows",
)


async def delete_ingestions_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    n = 0
    for table in _INGESTION_TABLES:
        n += await delete_scoped(table, org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch)
    return n


# A cancel the user asked for on another writer's open job still cancels it: the
# row is copied without the user's identity before the user's rows go.
_KEEP_OTHERS_CANCELS = (
    "INSERT INTO loom.knowledge_ingest_cancels (org, namespace, ingestion_id, requested_at)"
    " SELECT org, namespace, ingestion_id, requested_at FROM loom.knowledge_ingest_cancels"
    " WHERE org = {org:String} AND user_id = {uid:String}"
    " AND (namespace, ingestion_id) IN ("
    "SELECT namespace, ingestion_id FROM loom.knowledge_ingestions FINAL"
    " WHERE org = {org:String} AND user_id != {uid:String}"
    " AND state IN {open:Array(String)})"
)


async def delete_ingestions_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    ch = ch or _default_client()
    # Sequential: a replica behind the cancel or the job would copy nothing, and the
    # delete below still takes the cancel.
    with sequential_reads():
        await ch.execute(
            _KEEP_OTHERS_CANCELS, params={"org": org, "uid": user_id, "open": list(OPEN_STATES)}
        )
    n = 0
    for table in _INGESTION_TABLES:
        n += await count_then_mutate(
            table,
            count_where=ORG_UID,
            mutation="DELETE",
            mutation_where=ORG_UID,
            params={"org": org, "uid": user_id},
            final=False,
            ch=ch,
        )
    return n
