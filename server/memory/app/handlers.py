"""Memory plane handlers — long-tail RMT path against `loom.memory`.

memory.set additionally writes to `loom.memory_embeddings` when the type
is searchable (semantic, procedural, preference). Episodic memory is
session-scoped and not embedded.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from server.auth.context import current_auth
from server.code.app.member_writes import assert_own_row
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value_json as _content_sha256_of_stored
from server.embedding import get_embedder
from server.identity.domain.read_scope import request_read_filters
from server.memory.api.models_crud import (
    MemoryClearInput,
    MemoryClearOutput,
    MemoryDeleteInput,
    MemoryDeleteOutput,
    MemoryGetByIdInput,
    MemoryGetByIdOutput,
    MemoryGetInput,
    MemoryGetOutput,
    MemorySetInput,
    MemorySetOutput,
    MemoryTouchInput,
    MemoryTouchOutput,
    MemoryTouchResult,
    MemoryUnpinInput,
    MemoryUnpinOutput,
)
from server.memory.app import conflict_deferred as _cd
from server.memory.app.provenance import request_scope
from server.memory.app.write import handle_memory_set
from server.memory.domain import lock as _lock
from server.memory.domain import trust as _trust
from server.memory.domain.entity_sentinels import public_entities
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version
from server.settings import settings

log = logging.getLogger(__name__)


# Leaf module (no internal imports) shared with memory.app.retrieval to
# avoid a circular import.
from server.memory.domain.memory_types import is_searchable_type  # noqa: E402


def existing_context_limit(msgs: list[dict[str, str]], limit: int) -> int:
    """How many existing memories an extraction sees. A short transcript (the compact-prompt
    tier) states one or two facts, often an update of one already stored, and an update's
    target ranks 1st or 2nd of the namespace's facts, so it gets
    `memory_extract_existing_short_limit` rows (`limit` when that is 0). A long transcript
    gets none: shown the namespace's facts, the extractor links a later session's details to
    them instead of writing them, and the write path's conflict and canonical-merge checks
    already catch the true duplicates."""

    short = int(settings.memory_extract_existing_short_limit)
    tier = int(settings.memory_extract_compact_prompt_max_chars)
    chars = sum(len(str(m.get("content", ""))) for m in msgs)
    if tier <= 0 or chars > tier:
        return 0
    return min(limit, short) if short > 0 else limit


async def _fetch_relevant_existing_memories(
    identity: Any,  # memory_repo.Identity
    msgs: list[dict[str, str]],
    *,
    limit: int = 50,
) -> list[dict[str, str]]:
    """Retrieve the `limit` existing memories under `identity` most
    semantically relevant to `msgs`, as extractor cross-session dedup
    context (recency alone loses early sessions in long haystacks).

    Returns `{memory_key, value, content_sha256}` dicts. Fail-open:
    caller wraps in try/except; we never block extraction.
    """
    import server.memory.store as memory_repo
    from server.memory.app import retrieval as memory_search

    query_text = (
        " ".join(str(m.get("content", "")).strip() for m in msgs if m.get("role") == "user")[:1024]
        or " ".join(str(m.get("content", "")).strip() for m in msgs)[:1024]
    )
    if not query_text:
        return []

    top_k = existing_context_limit(msgs, limit)
    if top_k <= 0:
        return []
    embedder = get_embedder()
    query_vec = await embedder.embed(query_text)
    search_query = memory_search.SearchQuery(
        org=identity.org,
        namespace=identity.namespace,
        scope="private",
        agent=identity.agent,
        embedding_model=embedder.model_name,
        query_vec=query_vec,
        top_k=top_k,
        user_id=identity.user_id,
        query_text=query_text,
        search_mode="semantic",
        # The extractor can only update a fact key. Chunk hits would take about half of the
        # 50 slots and resolve to nothing, and the chunk leg would be a second query on every
        # extraction.
        execution_planes=["fact"],
        chunk_max_share=0.0,
    )
    hits = await memory_search.vector_search(search_query)
    if not hits:
        return []

    # Resolve the exact vector hits by key (not via memory.list, which caps
    # and would drop old-but-relevant memories in long-lived namespaces).
    hit_keys: list[str] = []
    seen: set[str] = set()
    for h in hits:
        if h.memory_key not in seen:
            seen.add(h.memory_key)
            hit_keys.append(h.memory_key)
    # The hits come from every session of this identity; resolve them across sessions too.
    # Keyed to the session being written, the lookup would return nothing for any row an
    # earlier session wrote, and most extraction calls are the first call of their session.
    any_session = memory_repo.Identity(
        org=identity.org,
        namespace=identity.namespace,
        agent=identity.agent,
        user_id=identity.user_id,
        session_id="*",
    )
    rows = await memory_repo.get_many_by_keys(any_session, hit_keys, scope=request_scope())
    by_key = {r.memory_key: r for r in rows}
    out: list[dict[str, str]] = []
    for h in hits:
        r = by_key.get(h.memory_key)
        if r is None:
            continue
        # sha over canonical JSON so the caller can hash-dedup extractions.
        sha = _content_sha256_of_stored(
            json.dumps(r.value, default=str) if r.value is not None else "",
        )
        out.append(
            {
                "memory_key": h.memory_key,
                "value": str(r.value)[:200] if r.value is not None else "",
                "content_sha256": sha,
            }
        )
    return out


def split_chunk_key(memory_key: str) -> tuple[str, int | None]:
    """`chunk:<session_id>:<chunk_index>` → (session_id, chunk_index); ("", None)
    when the key is not a chunk id. The session id keeps its own colons."""
    if not memory_key.startswith("chunk:"):
        return "", None
    body = memory_key[len("chunk:") :]
    session_id, sep, idx_s = body.rpartition(":")
    if not sep or not session_id:
        return "", None
    try:
        return session_id, int(idx_s)
    except ValueError:
        return "", None


def chunk_parent_key(memory_key: str) -> str:
    """The parent memory key a memory.set chunk id names, "" for anything else. memory.set
    writes a key's chunks under the session id `memory.set:<memory_key>`, so the parent is
    readable from the id alone."""
    session_id, idx = split_chunk_key(memory_key)
    if idx is None or not session_id.startswith("memory.set:"):
        return ""
    return session_id[len("memory.set:") :]


async def _get_chunk_with_neighbors(input: MemoryGetInput) -> MemoryGetOutput:
    """Resolve a `chunk:{session_id}:{chunk_index}` key from
    loom.memory_chunks, plus chunk_index ±1 neighbors, stitched into the
    `value` field so callers get a coherent passage."""
    # `chunk:<session_id>:<chunk_index>` — the session id may itself carry colons
    # (`memory.set:<memory_key>`), so the index is the last field, not the third.
    session_id, idx = split_chunk_key(input.memory_key)
    if not session_id or idx is None:
        return MemoryGetOutput(found=False)

    # The read's user bound — the body's user, or the one a token is pinned to
    # (read_user_id; a body naming another user is refused, as memory.search
    # refuses it) — keeps the chunk and its neighbours to that user's rows;
    # chunks stamped '-' (no user) are not served under a bound.
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import resolve_read_filters

    bound = resolve_read_filters(
        requested="namespace", user_id=input.user_id, session_id=None, auth=current_auth()
    ).user_id
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "sid": session_id,
        "lo": max(0, idx - 1),
        "hi": idx + 1,
    }
    user_pred = ""
    if bound is not None:
        user_pred = "AND user_id = {uid:String}"
        params["uid"] = bound
    ch = get_client()
    rows = await ch.query_all(
        f"""
        SELECT chunk_index, content, parent_memory_key
        FROM loom.memory_chunks FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND session_id = {{sid:String}}
          AND chunk_index BETWEEN {{lo:UInt32}} AND {{hi:UInt32}}
          {user_pred}
          AND tombstoned_at IS NULL
        ORDER BY chunk_index ASC
        """,
        params=params,
    )
    if not rows:
        return MemoryGetOutput(found=False)

    target = next((r for r in rows if int(r["chunk_index"]) == idx), None)
    if target is None:
        return MemoryGetOutput(found=False)

    stitched = "\n\n".join(str(r["content"]) for r in rows)

    return MemoryGetOutput(
        found=True,
        memory_type="episodic",
        value={
            "session_id": session_id,
            "chunk_index": idx,
            "parent_memory_key": str(target.get("parent_memory_key") or ""),
            "content": stitched,
            "neighbor_count": len(rows) - 1,
        },
        confidence=1.0,
        version=0,
        pinned=False,
        tags=[],
        entities=[],
    )


def same_user(row: Any, identity: Any) -> bool:
    """A namespace-wide (`agent="*"`) lookup drops every identity filter in the repository,
    so a row it returns is accepted only when it belongs to the caller's user. `user_id`
    partitions memory (tests/integration/test_memory_get.py::test_per_user_isolation); the
    agent does not."""
    return str(getattr(row, "user_id", "") or "") == str(getattr(identity, "user_id", "") or "")


async def _point_lookup(identity: Any, input: MemoryGetInput) -> Any:
    """The row at `input.memory_key` for this identity, else the same user's
    row under any agent.

    A get names a key, not the agent that wrote it: a Claude Code session (agent
    `claude-code`) asking for a key loom_search just served must find the row even when the
    MCP default agent wrote it. The exact identity is tried first so a caller's own row wins
    over a namesake; the `*` form is the fallback, filtered to the caller's user."""
    import server.memory.store as memory_repo
    from server.observability.otel import traced

    strong = bool(getattr(input, "_strong_read", False))  # procedure RMW: latest version
    with traced("ch.query.memory", op="point_lookup", memory_key=input.memory_key):
        row = await memory_repo.get_by_key(
            identity,
            input.memory_key,
            as_of=input.as_of,
            include_usage_totals=True,
            fresh=True,  # the SDK pattern is set → get; a miss re-reads once
            strong=strong,
            include_expired=False,  # an expired fact reads as gone, like search
            scope=request_read_filters(current_auth()),
        )
    if row is not None or identity.agent == "*":
        return row
    wide = memory_repo.Identity(
        org=identity.org,
        namespace=identity.namespace,
        agent="*",
        user_id=identity.user_id,
        session_id=identity.session_id,
    )
    with traced("ch.query.memory", op="point_lookup_wide", memory_key=input.memory_key):
        row = await memory_repo.get_by_key(
            wide,
            input.memory_key,
            as_of=input.as_of,
            include_usage_totals=True,
            fresh=True,
            strong=strong,
            include_expired=False,
            scope=request_read_filters(current_auth()),
        )
    return row if row is not None and same_user(row, identity) else None


async def handle_memory_get(input: MemoryGetInput) -> MemoryGetOutput:
    # Exact-key reads are still reads: apply the same token read-scope ceiling
    # as search before constructing the point-lookup identity. Without this,
    # a token pinned to one end user could name another user in the body and
    # retrieve that row by key.
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import resolve_read_filters
    from server.operations import read_projection

    filters = resolve_read_filters(
        requested="namespace",
        user_id=input.user_id,
        session_id=input.session_id,
        auth=current_auth(),
    )
    identity_updates: dict[str, str] = {}
    if filters.user_id is not None and filters.user_id != input.user_id:
        identity_updates["user_id"] = filters.user_id
    if filters.session_id is not None and filters.session_id != input.session_id:
        identity_updates["session_id"] = filters.session_id
    if identity_updates:
        input = input.model_copy(update=identity_updates)

    # Resolve the field whitelist before the read so an unknown name is a 422 that cost no
    # statement; None = every field.
    keep = read_projection.resolve(
        MemoryGetOutput,
        list(input.fields or []),
        always=read_projection.GET_ALWAYS,
    )
    from server.core.ch import own_write_reads

    with own_write_reads():  # the value a set/update just wrote
        out = await _handle_memory_get(input)
    out._projection = keep
    if out.found and not out.unchanged:  # an unchanged poll exposes no body
        from server.memory.app.query import _record_read_exposure

        _record_read_exposure("memory.get", input, [input.memory_key])
    return out


async def _handle_memory_get(input: MemoryGetInput) -> MemoryGetOutput:
    import server.memory.store as memory_repo

    # Chunk-prefixed keys come from memory.search chunk hits; route to
    # loom.memory_chunks (with ±1 neighbors), not loom.memory.
    if input.memory_key.startswith("chunk:"):
        return await _get_chunk_with_neighbors(input)

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    row = await _point_lookup(identity, input)
    if row is None:
        return MemoryGetOutput(found=False)

    # `follow` serves the canonical a duplicate points to.
    resolved_from: str | None = None
    if input.follow:
        from server.memory.app.query import follow_duplicate

        row, resolved_from = await follow_duplicate(input, row)

    # The caller holds this version already; say so and stop.
    # No body, no sensitive-read audit (nothing was read out), no breakdown.
    if input.if_version is not None and int(row.version or 0) == int(input.if_version):
        return MemoryGetOutput(
            found=True,
            unchanged=True,
            memory_id=str(row.memory_id),
            version=row.version,
            memory_type=row.memory_type,
            updated_at=getattr(row, "updated_at", None) or row.created_at,
            pinned=bool(getattr(row, "pinned", False)),
        )

    if str(getattr(row, "sensitivity", "") or "") == "high":
        # Key-only reads of a 'high' row are the one path that returns it;
        # each one is an audit row. Never raises.
        from server.observability import audit as _audit

        await _audit.emit(
            "memory.sensitive_read",
            org=input.org,
            namespace=input.namespace,
            actor=input.agent,
            subject={"memory_key": input.memory_key, "memory_id": str(row.memory_id)},
        )

    # content_sha256 isn't stored on loom.memory; compute from canonical
    # JSON so the wire shape matches the SDK precondition.
    sha = (
        _content_sha256_of_stored(json.dumps(row.value, default=str))
        if row.value is not None
        else ""
    )
    recall_by_agent: list[_RecallByAgent] = []
    if input.include_recall_breakdown:
        # Keyed on row.memory_id (resolved against the full identity tuple);
        # a re-query by memory_key alone could hit a different row under
        # cross-identity key reuse. Empty/placeholder agents excluded.
        try:
            from server.memory.api.models_crud import _RecallByAgent

            agent_rows = await get_client().query_all(
                """
                SELECT agent,
                       sum(event_count) AS n,
                       max(ts) AS last_at
                FROM loom.memory_usage_events
                WHERE org = {org:String}
                  AND namespace = {ns:String}
                  AND memory_id = {mid:String}
                  AND event_type = 'recall'
                  AND agent != ''
                  AND agent != '*'
                  AND agent != '-'
                  AND ts > now() - INTERVAL 30 DAY
                GROUP BY agent
                ORDER BY n DESC
                """,
                # The table is sorted (org, namespace, memory_id, ts); a filter on memory_id
                # alone would scan every row it holds (12 MB and three statements per drawer
                # open for a list of a few chips).
                params={"org": row.org, "ns": row.namespace, "mid": str(row.memory_id)},
            )
            recall_by_agent = [
                _RecallByAgent(
                    agent=str(r["agent"]),
                    count=int(r["n"]),
                    last_at=r["last_at"],
                )
                for r in agent_rows
            ]
        except Exception as e:  # noqa: BLE001 — recall_by_agent is optional detail on memory.get
            log.warning("recall_by_agent lookup failed (non-fatal): %s", e)

    from server.memory.app.query import _canonical_keys_for_rows

    canonical = await _canonical_keys_for_rows(input.org, input.namespace, [row])
    return MemoryGetOutput(
        found=True,
        canonical_key=(
            str(row.memory_key) if resolved_from is not None else canonical.get(input.memory_key)
        ),
        resolved_from=resolved_from,
        memory_id=str(row.memory_id),
        value=row.value,
        memory_type=row.memory_type,
        confidence=row.confidence,
        version=row.version,
        created_at=row.created_at,
        updated_at=getattr(row, "updated_at", None) or row.created_at,
        expires_at=row.expires_at,
        on_expire=getattr(row, "on_expire", "") or "",
        expire_reason=getattr(row, "expire_reason", "") or "",
        valid_at=getattr(row, "valid_at", None),
        valid_to=getattr(row, "valid_to", None),
        temporal_anchor=getattr(row, "temporal_anchor", None),
        temporal_confidence=float(getattr(row, "temporal_confidence", 0.0) or 0.0),
        char_limit=getattr(row, "char_limit", None),
        pinned=row.pinned,
        sensitivity=str(getattr(row, "sensitivity", "") or ""),
        agent=getattr(row, "agent", "") or "",
        status=getattr(row, "status", None),
        superseded_by=str(getattr(row, "superseded_by", "") or ""),
        duplicate_of=str(getattr(row, "duplicate_of", "") or ""),
        conflict_verdict=_cd.verdict_from_row(
            str(row.memory_id), getattr(row, "status", None), getattr(row, "duplicate_of", "")
        ),
        content_sha256=sha,
        tags=list(getattr(row, "tags", None) or []),
        entities=public_entities(getattr(row, "entities", None)),
        recall_count=getattr(row, "recall_count", 0) or 0,
        last_recalled_at=getattr(row, "last_recalled_at", None),
        recall_by_agent=recall_by_agent,
        verified_by=getattr(row, "verified_by", "") or "",
        actor=getattr(row, "actor", "") or "",
        source_class=getattr(row, "source_class", "") or "",
        **_lock.lock_state(row),
        verified_at=getattr(row, "verified_at", None),
        verification=_trust.verification_state(
            getattr(row, "verified_by", ""), getattr(row, "verified_sha256", ""), sha
        ),
    )


async def handle_memory_pin(input: MemorySetInput) -> MemorySetOutput:
    """Explicit-save variant. Forces `pinned=True`; otherwise shares the
    `memory.set` write path."""
    input.pinned = True
    return await handle_memory_set(input)


async def handle_memory_unpin(input: MemoryUnpinInput) -> MemoryUnpinOutput:
    """Lift a pin by writing one full carried-forward RMT version row."""
    import server.memory.store as memory_repo

    ch = get_client()
    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    existing = await memory_repo.get_by_key(
        identity, input.memory_key, fresh=True, strong=True, scope=request_scope()
    )
    if existing is None and identity.agent != "*":
        wide = memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent="*",
            user_id=input.user_id,
            session_id=input.session_id,
        )
        existing = await memory_repo.get_by_key(
            wide, input.memory_key, fresh=True, strong=True, scope=request_scope()
        )
        if existing is not None and not same_user(existing, identity):
            existing = None
    if existing is None:
        return MemoryUnpinOutput(unpinned=False, reason="not_found")
    assert_own_row(existing, op="memory.unpin")
    if not bool(getattr(existing, "pinned", False)):
        return MemoryUnpinOutput(
            unpinned=False,
            reason="not_pinned",
            memory_id=str(existing.memory_id),
        )
    version = next_version()
    row = memory_repo.build_memory_version_row(
        existing,
        version=version,
        updated_at=memory_repo.written_now(),
        pinned=False,
    )
    await insert_memory_versions([row], ch=ch)
    return MemoryUnpinOutput(
        unpinned=True,
        memory_id=str(existing.memory_id),
        version=version,
    )


async def handle_memory_delete(input: MemoryDeleteInput) -> MemoryDeleteOutput:
    """Soft-delete: write a tombstone row with a higher version, so RMT
    collapse picks it and `memory.get`'s `tombstoned_at IS NULL` filter
    excludes it. Copies the live row's columns forward. Preserves the row
    for the grace window; avoids expensive CH mutations."""
    import server.memory.store as memory_repo
    from server.observability.otel import traced

    ch = get_client()
    version = next_version()
    now = datetime.now(UTC)

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    with traced("ch.query.memory.precheck", op="tombstone_lookup"):
        # fresh: a delete right after a set must not no-op on a replica lag miss.
        existing = await memory_repo.get_by_key(
            identity, input.memory_key, fresh=True, strong=True, scope=request_scope()
        )
    if existing is None and identity.agent != "*":
        # A delete names a key, not the identity that wrote it (hooks write under their own
        # agent/session; a chat client deleting the key holds another), and an exact-identity
        # miss would read as success. Look namespace-wide.
        wide = memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent="*",
            user_id=input.user_id,
            session_id=input.session_id,
        )
        existing = await memory_repo.get_by_key(
            wide, input.memory_key, fresh=True, strong=True, scope=request_scope()
        )
        if existing is not None and not same_user(existing, identity):
            existing = None  # the wide form ignores user_id; another user's row is not ours
    if existing is None:
        # Idempotent: deleting a non-existent row is a no-op — and says so.
        return MemoryDeleteOutput(tombstoned=False, found=False, version=version)
    _lock.assert_unlocked(existing, "delete", op="memory.delete")
    assert_own_row(existing, op="memory.delete")

    # build_memory_version_row carries every live column forward (avoiding
    # silent column loss); only the tombstone deltas override.
    tombstone_row = memory_repo.build_memory_version_row(
        existing,
        version=version,
        updated_at=memory_repo.written_now(),
        tombstoned_at=now,
        source_trace="",
        expires_at=None,
    )
    with traced("ch.insert.memory.tombstone", table="loom.memory"):
        await insert_memory_versions([tombstone_row], ch=ch)

    # Propagate the tombstone to memory_embeddings — search reads that table, so without it
    # soft-deleted memories keep surfacing. Each live row of the key (one per scope and
    # embedding model) is re-emitted with every column and its vectors carried, so it
    # collapses onto the original under the RMT sort key. Every memory type takes this path:
    # the set paths skip the row for a non-searchable type, but the table can hold one anyway
    # (dim-0 stubs, rows other paths wrote). A key with no live row writes nothing.
    try:
        await memory_repo.tombstone_embeddings_for_key(
            input.org,
            input.namespace,
            existing.agent,  # the row's own identity, which may differ from the caller's
            existing.user_id,
            existing.session_id,
            input.memory_key,
            ts=now.replace(tzinfo=None) if now.tzinfo else now,
            ch=ch,
        )
    except Exception as e:  # noqa: BLE001 — the memory row tombstone already landed; mirror is best-effort
        log.warning(
            "memory_embeddings tombstone write failed (non-fatal): %s",
            e,
        )

    # The verbatim chunks a long value wrote under this key go with it, or search keeps
    # lifting a deleted fact through its own body.
    if is_searchable_type(existing.memory_type):
        from server.memory.app import set_chunks as _set_chunks

        await _set_chunks.tombstone_row_chunks_fail_open(existing, ch=ch)

    # A procedure's run ledger must not outlive the procedure.
    # Best-effort: the tombstone above is the erasure of record; a ledger
    # failure is logged, not raised, and namespace erasure sweeps it anyway.
    if getattr(existing, "memory_type", "") == "procedural":
        try:
            await memory_repo.delete_procedure_runs_by_key(
                input.org, input.namespace, input.memory_key
            )
        except Exception as e:  # noqa: BLE001 — the tombstone stands
            log.warning(
                "memory.delete: procedure_runs cascade failed for %s: %s", input.memory_key, e
            )
    # A summary built from this row now says something its evidence no
    # longer supports. If none of its sources survive it goes too; otherwise
    # it is downgraded to `conflicted` (review queue) with a
    # `stale-source:<key>` tag. Best effort — the delete never fails on it.
    dep_tomb = dep_down = 0
    try:
        dep_tomb, dep_down = await memory_repo.downgrade_consolidated_dependents(
            input.org,
            input.namespace,
            input.memory_key,
            ch=ch,
        )
    except Exception as e:  # noqa: BLE001 — the tombstone stands
        log.warning(
            "memory.delete: dependent summaries not downgraded for %s: %s",
            input.memory_key,
            e,
        )
    return MemoryDeleteOutput(
        tombstoned=True,
        version=version,
        dependents_tombstoned=dep_tomb,
        dependents_downgraded=dep_down,
    )


async def handle_memory_clear(input: MemoryClearInput) -> MemoryClearOutput:
    """Bulk-tombstone for `memory.clear`.

    Defenses: at least one of `user_id` / `agent` is required (a scope-less
    clear is `erasure.request_namespace`'s job), and `confirm_subject` must
    match one of them (guard against UI-dropdown accidents).

    Same tombstone semantics as `memory.delete`.
    """
    import server.memory.store as memory_repo

    if input.user_id is None and input.agent is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_SCOPE",
                "message": (
                    "memory.clear requires at least one of `user_id` "
                    "or `agent`. For namespace-wide clearing use "
                    "erasure.request_namespace (full saga, cross-system "
                    "cascade, auditable)."
                ),
            },
        )
    candidates = [v for v in (input.user_id, input.agent) if v is not None]
    if input.confirm_subject not in candidates:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRM_SUBJECT_MISMATCH",
                "message": (
                    "`confirm_subject` must equal the user_id or agent "
                    "being cleared. This is a mid-weight defense against "
                    "destructive UI-dropdown accidents."
                ),
            },
        )

    counts = await memory_repo.bulk_tombstone_scope(
        org=input.org,
        namespace=input.namespace,
        user_id=input.user_id,
        agent=input.agent,
    )
    locked_skipped = int(counts.pop("locked_skipped", 0) or 0)
    return MemoryClearOutput(
        cleared_at=naive_utc_now(),
        rows_affected=counts,
        total=sum(counts.values()),
        locked_skipped=locked_skipped,
    )


async def handle_memory_touch(input: MemoryTouchInput) -> MemoryTouchOutput:
    """Change only the expiry: write a new RMT(version) row with the same
    value and every other column, and the new (or cleared)
    expires_at. Cheaper than a full set, and it leaves entities, conflict
    state and reinforcement untouched. `memory_keys` does the same for many
    keys of one identity in one read, one insert and one batched embedding
    re-emit."""
    import server.memory.store as memory_repo

    ch = get_client()
    ident = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    if input.memory_keys:
        wanted = list(dict.fromkeys(k for k in input.memory_keys if k))
        rows = await memory_repo.get_many_by_keys(
            ident, wanted, ch=ch, strong=True, scope=request_scope()
        )
    else:
        wanted = [input.memory_key]
        one = await memory_repo.get_by_key(
            ident,
            input.memory_key,
            fresh=True,  # touch follows set
            strong=True,  # the head feeds a write
            scope=request_scope(),
        )
        rows = [one] if one is not None else []
    by_key = {r.memory_key: r for r in rows}
    for r in rows:
        _lock.assert_unlocked(r, "expire", op="memory.touch")

    new_expires_at = None
    if not input.clear_expiry:
        new_expires_at = datetime.now(UTC) + timedelta(seconds=input.ttl_seconds or 0)
    naive_expiry = new_expires_at.replace(tzinfo=None) if new_expires_at else None
    touch_rows: list[dict[str, Any]] = []
    versions: dict[str, int] = {}
    for key in wanted:
        existing = by_key.get(key)
        if existing is None:
            continue
        version = next_version()
        versions[key] = version
        # Mirror every persisted column; only the touch deltas override.
        overrides: dict[str, Any] = {"version": version, "expires_at": new_expires_at}
        if input.on_expire:
            overrides["on_expire"] = input.on_expire
        if input.expire_reason:
            overrides["expire_reason"] = input.expire_reason[:512]
        touch_rows.append(memory_repo.build_memory_version_row(existing, **overrides))
    if touch_rows:
        await insert_memory_versions(touch_rows, ch=ch)
        # search filters on the denormalized memory_embeddings.expires_at, so mirror the new
        # expiry there or a touched row vanishes from search at its old expiry. A versioned
        # re-emit of the keys' live embedding rows, not an in-place UPDATE (which would leave
        # a patch part on the vector table); many keys go as one batch. Non-fatal: the
        # loom.memory write above already committed.
        mirror = [
            (
                (r.org, r.namespace, r.agent, r.user_id, r.session_id, r.memory_key),
                {"expires_at": naive_expiry},
            )
            for r in (by_key[k] for k in versions)
            if is_searchable_type(r.memory_type)
        ]
        if mirror:
            try:
                if input.memory_keys:
                    await memory_repo.reemit_embedding_rows_for_keys(mirror, ch=ch)
                else:  # one key: the keyed read the single-key path always ran
                    (o, n, a, u, s_, k), ov = mirror[0]
                    await memory_repo.reemit_embedding_rows_for_key(o, n, a, u, s_, k, ch=ch, **ov)
            except Exception as e:  # noqa: BLE001 — expiry mirror is best-effort; memory rows already updated
                log.warning("memory_embeddings expiry mirror failed (non-fatal): %s", e)

    results = [
        MemoryTouchResult(
            memory_key=k,
            touched=k in versions,
            version=versions.get(k),
            expires_at=naive_expiry if k in versions else None,
        )
        for k in wanted
    ]
    if not input.memory_keys:
        r0 = results[0]
        return MemoryTouchOutput(touched=r0.touched, version=r0.version, expires_at=r0.expires_at)
    return MemoryTouchOutput(
        touched=bool(versions),
        expires_at=naive_expiry if versions else None,
        results=results,
        touched_count=len(versions),
    )


async def handle_memory_get_by_id(input: MemoryGetByIdInput) -> MemoryGetByIdOutput:
    """Resolve an opaque memory_id to the full memory row (latest version,
    non-tombstoned). Returns found=False if no row matches."""
    import server.memory.store as memory_repo

    row = await memory_repo.get_by_id(
        input.memory_id,
        org=input.org,
        as_of=getattr(input, "as_of", None),
        include_usage_totals=True,
        fresh=True,
        scope=request_read_filters(current_auth()),
    )
    if row is None:
        return MemoryGetByIdOutput(found=False)
    # Addressed by an org-wide id: the transport had no namespace to bind, so
    # the row's own namespace is checked here and a foreign row is hidden, not
    # refused.
    from server.auth.authz import row_in_scope

    if not await row_in_scope(input.org, row.namespace):
        return MemoryGetByIdOutput(found=False)
    sha = (
        _content_sha256_of_stored(json.dumps(row.value, default=str))
        if row.value is not None
        else ""
    )
    if str(getattr(row, "sensitivity", "") or "") == "high":
        # Key-only read of a 'high' row: audited. Never raises.
        from server.observability import audit as _audit

        await _audit.emit(
            "memory.sensitive_read",
            org=input.org,
            namespace=row.namespace,
            # get_by_id carries no caller agent; the row's writer is the best
            # available actor for the ledger.
            actor=getattr(input, "agent", "") or row.agent or "-",
            subject={"memory_id": str(row.memory_id), "memory_key": row.memory_key},
        )
    return MemoryGetByIdOutput(
        found=True,
        sensitivity=str(getattr(row, "sensitivity", "") or ""),
        memory_id=row.memory_id,
        namespace=row.namespace,
        agent=row.agent,
        user_id=row.user_id,
        session_id=row.session_id,
        memory_key=row.memory_key,
        memory_type=row.memory_type,
        value=row.value,
        confidence=row.confidence,
        version=row.version,
        created_at=row.created_at,
        expires_at=row.expires_at,
        on_expire=getattr(row, "on_expire", "") or "",
        expire_reason=getattr(row, "expire_reason", "") or "",
        valid_at=getattr(row, "valid_at", None),
        valid_to=getattr(row, "valid_to", None),
        pinned=row.pinned,
        content_sha256=sha,
        verified_by=getattr(row, "verified_by", "") or "",
        actor=getattr(row, "actor", "") or "",
        source_class=getattr(row, "source_class", "") or "",
        **_lock.lock_state(row),
        verified_at=getattr(row, "verified_at", None),
        verification=_trust.verification_state(
            getattr(row, "verified_by", ""), getattr(row, "verified_sha256", ""), sha
        ),
    )
