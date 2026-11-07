"""Memory plane handlers — long-tail RMT path against `loom.memory`.

memory.set additionally writes to `loom.memory_embeddings` when the type
is searchable (semantic, procedural, preference). Episodic memory is
session-scoped and not embedded.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from server.auth.context import current_auth
from server.code.app.member_writes import assert_own_row
from server.core import pg
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.error_summary import error_summary
from server.core.errors import BackendUnavailableError, ValidationError
from server.core.hashing import served_content_sha256
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import (
    CodeLink,
    MemoryClearInput,
    MemoryClearOutput,
    MemoryDeleteInput,
    MemoryDeleteOutput,
    MemoryDeleteVersionInput,
    MemoryDeleteVersionOutput,
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
from server.memory.app import webhooks
from server.memory.app.get_output import duplicate_of_shown, row_output
from server.memory.app.provenance import writer_actor
from server.memory.app.write import handle_memory_set
from server.memory.domain import lock as _lock
from server.memory.domain import trust as _trust
from server.memory.domain.delete_marks import delete_version_trace
from server.memory.domain.served import served_totals
from server.memory.domain.webhook_events import memory_change
from server.memory.store import key_write_lock
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import Identity, next_version, version_over
from server.observability import degradations
from server.observability.budget_shape import note_budget_shape
from server.settings import settings

log = logging.getLogger(__name__)


# Leaf module (no internal imports) shared with memory.app.retrieval to
# avoid a circular import.
from server.memory.domain.memory_types import is_embedded, is_searchable_type  # noqa: E402
from server.memory.domain.sensitivity import reads_high, value_reads_high  # noqa: E402


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
    # A 'high' row never goes into the extraction prompt, which is sent to the LLM provider;
    # the read leaves them out before it picks one per key, so another session's 'high' row
    # hides no plain one.
    rows = await memory_repo.get_many_by_keys(
        any_session, hit_keys, scope=request_scope(), exclude_high_sensitivity=True
    )
    by_key = {r.memory_key: r for r in rows}
    out: list[dict[str, str]] = []
    for h in hits:
        r = by_key.get(h.memory_key)
        # A row whose value reads as high, whatever its label, is not shown to the model.
        if r is None or value_reads_high(r.value):
            continue
        # sha over canonical JSON so the caller can hash-dedup extractions.
        sha = served_content_sha256(r.value)
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
        SELECT chunk_index, content, parent_memory_key, embedding_dim
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

    # A chunk that reads as high, or that was written without a vector for holding part
    # of a high shape, is served only when its own key is read, and that read is audited
    # as a key read of a 'high' row is. A neighbour is context the caller did not name: a
    # held one is left out of the passage.
    def held(row: dict[str, Any]) -> bool:
        return int(row["embedding_dim"]) == 0 or reads_high(str(row["content"]))

    if held(target):
        from server.observability import audit as _audit

        note_budget_shape("audited")
        await _audit.emit(
            "memory.sensitive_read",
            org=input.org,
            namespace=input.namespace,
            actor=writer_actor(input.agent),
            subject={"memory_key": input.memory_key, "session_id": session_id, "chunk_index": idx},
        )
    rows = [r for r in rows if r is target or not held(r)]
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
            scope=request_scope(),
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
            scope=request_scope(),
        )
    return row if row is not None and same_user(row, identity) else None


# What `memory.get` reports for a row from its lifecycle status.
_STATUS_VERDICT = {
    "active": "clear",
    "conflicted": "conflict",
    "duplicate": "duplicate",
    "superseded": "superseded",
    "rejected": "rejected",
    "tombstoned": "tombstoned",
}


def _verdict_from_status(status: Any, duplicate_of: Any = "") -> str:
    """The verdict a row shows on read, from its lifecycle status (`active` →
    `clear`, `conflicted` → `conflict`, an active row pointing at its original →
    `duplicate`, …)."""
    s = str(status or "")
    if s == "active" and str(duplicate_of or ""):
        return "duplicate"
    return _STATUS_VERDICT.get(s, s)


async def handle_memory_get(input: MemoryGetInput) -> MemoryGetOutput:
    # Exact-key reads are still reads: apply the same token read-scope ceiling
    # as search before constructing the point-lookup identity. Without this,
    # a token pinned to one end user could name another user in the body and
    # retrieve that row by key.
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

        note_budget_shape("audited")
        await _audit.emit(
            "memory.sensitive_read",
            org=input.org,
            namespace=input.namespace,
            # The reader, from the token: a bulk or wildcard read names no agent.
            actor=writer_actor(input.agent),
            subject={"memory_key": input.memory_key, "memory_id": str(row.memory_id)},
        )

    # content_sha256 isn't stored on loom.memory; compute from canonical
    # JSON so the wire shape matches the SDK precondition.
    sha = served_content_sha256(row.value)
    recall_by_agent: list[_RecallByAgent] = []
    served: dict[str, Any] = {}
    if input.include_recall_breakdown:
        # Keyed on the key's live rows inside the reader's scope: an agent read puts its
        # event on the newest copy of the key in its own scope, which need not be the row
        # this read resolved. A reader narrowed to one user never counts another's copy.
        try:
            from server.memory.api.models_crud import _RecallByAgent
            from server.memory.store.served import served_by_agent

            agent_rows = await served_by_agent(
                row.org, row.namespace, row.memory_key, scope=request_scope()
            )
            served = served_totals(agent_rows, row.recall_count, row.last_recalled_at)
            recall_by_agent = [
                _RecallByAgent(agent=str(r["agent"]), count=int(r["n_30d"]), last_at=r["last_at"])
                for r in sorted(agent_rows, key=lambda r: -int(r["n_30d"]))
                if int(r["n_30d"]) > 0
            ]
        except Exception as e:  # noqa: BLE001 — the served figures are optional detail on memory.get
            log.warning("served lookup failed (non-fatal): %s", e)

    from server.memory.app.query import _canonical_keys_for_rows

    canonical = await _canonical_keys_for_rows(input.org, input.namespace, [row])
    duplicate_of = duplicate_of_shown(row, canonical.get(input.memory_key))
    from server.memory.app.code_link_reads import head_of, links_of
    from server.memory.app.served_superseded import shown_successors

    wants_code = not input.fields or "code" in input.fields

    async def code() -> list[CodeLink]:
        return await links_of(input.org, head_of(row)) if wants_code else []

    successors, links = await asyncio.gather(
        shown_successors(input.org, input.namespace, [row]), code()
    )
    return row_output(
        row,
        sha=sha,
        canonical_key=(
            str(row.memory_key) if resolved_from is not None else canonical.get(input.memory_key)
        ),
        duplicate_of=duplicate_of,
        superseded_by=successors.get(str(row.memory_id), ""),
        code=links,
        resolved_from=resolved_from,
        conflict_verdict=_verdict_from_status(row.status, duplicate_of),
        recall_by_agent=recall_by_agent,
        **served,
    )


async def handle_memory_pin(input: MemorySetInput) -> MemorySetOutput:
    """Explicit-save variant. Forces `pinned=True`; otherwise shares the
    `memory.set` write path."""
    input.pinned = True
    return await handle_memory_set(input, op="memory.pin", retry_what="the pin")


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
    # Read again and written under the key's lock, as a pin and a resolver write are: a
    # retirement landing between the read and the unpinned copy would be written over.
    from server.memory.store.key_write_lock import key_lock_for

    async with key_lock_for(
        input.org,
        existing.namespace,
        existing.agent,
        existing.user_id,
        existing.session_id,
        existing.memory_key,
        what="the unpin",
        operation="memory.unpin",
    ):
        head = await memory_repo.get_by_id(
            str(existing.memory_id),
            org=input.org,
            namespace=existing.namespace,
            scope=WHOLE_NAMESPACE,
            fresh=True,
            strong=True,
        )
        if head is None:
            return MemoryUnpinOutput(unpinned=False, reason="not_found")
        if not bool(getattr(head, "pinned", False)):
            return MemoryUnpinOutput(
                unpinned=False, reason="not_pinned", memory_id=str(head.memory_id)
            )
        version = version_over(head)
        row = memory_repo.build_memory_version_row(
            head, version=version, updated_at=memory_repo.written_now(), pinned=False
        )
        await memory_repo.insert_versions_over_heads([row], [head], ch=ch)
    return MemoryUnpinOutput(unpinned=True, memory_id=str(head.memory_id), version=version)


async def handle_memory_delete(input: MemoryDeleteInput) -> MemoryDeleteOutput:
    """Soft-delete: write a tombstone row with a higher version, so RMT
    collapse picks it and `memory.get`'s `tombstoned_at IS NULL` filter
    excludes it. Copies the live row's columns forward. Preserves the row
    for the grace window; avoids expensive CH mutations."""
    import server.memory.store as memory_repo
    from server.observability.otel import traced

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
        return MemoryDeleteOutput(tombstoned=False, found=False, version=next_version())
    _lock.assert_unlocked(existing, "delete", op="memory.delete")
    assert_own_row(existing, op="memory.delete")
    # The version is drawn here, right before the insert: the vector retire skips rows at
    # or above it, so the later it is drawn the fewer late vector writes it leaves live.
    version = version_over(existing)
    await _write_tombstone_row(existing, version=version)
    try:
        tombstoned, downgraded = await _retire_derived(
            existing, version=version, op="memory.delete"
        )
    finally:
        # The tombstone is in: a retry finds no live row and returns found=False, so the
        # event is recorded whatever fails while retiring what was derived from it.
        if await webhooks.announce([("memory.tombstoned", memory_change(existing, version))]):
            note_budget_shape("webhook")
    return MemoryDeleteOutput(
        tombstoned=True,
        version=version,
        dependents_tombstoned=tombstoned,
        dependents_downgraded=downgraded,
    )


async def handle_memory_delete_version(
    input: MemoryDeleteVersionInput,
) -> MemoryDeleteVersionOutput:
    """Tombstone one version of a key under this exact identity, or nothing when the row
    under it has moved on. Conditional writes of the key wait for the key lock. A plain
    write takes none: one that committed after the version check is put back above the
    tombstone here, and one still in flight lifts itself above it when it lands
    (`write_fence`). Either way the write after the named version stays."""
    import server.memory.store as memory_repo

    if input.agent == "*":
        raise ValidationError("memory.delete_version needs a concrete agent, not '*'")
    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    try:
        async with key_write_lock.key_write_locks.hold(
            input.org,
            input.namespace,
            input.agent,
            input.user_id,
            input.session_id,
            input.memory_key,
        ):
            return await _delete_version_locked(input, identity)
    except _DeleteDeferred:
        # Written once the key lock is released: the insert may wait out the same outage.
        await _audit_deferred(input)
        raise


async def _delete_version_locked(
    input: MemoryDeleteVersionInput, identity: Identity
) -> MemoryDeleteVersionOutput:
    """`handle_memory_delete_version` under the key lock."""
    import server.memory.store as memory_repo

    existing = await memory_repo.get_by_key(
        identity, input.memory_key, fresh=True, strong=True, scope=request_scope()
    )
    if existing is None:
        # A retry of a delete of this version whose check is owed (it answered 503)
        # runs the check now and answers as the delete would have.
        check = await memory_repo.find_delete_check(
            input.org,
            input.namespace,
            input.agent,
            input.user_id,
            input.session_id,
            input.memory_key,
            checked=int(input.version),
        )
        if check is not None and not check["done"]:
            try:
                answer = await rerun_delete_check(check)
            except Exception as e:  # still owed: the next retry or the worker runs it
                raise _check_deferred(e) from e
            if answer is not None:
                return answer
        return MemoryDeleteVersionOutput(tombstoned=False, found=False)
    if existing.version != input.version:
        return MemoryDeleteVersionOutput(tombstoned=False, reason="version_changed")
    _lock.assert_unlocked(existing, "delete", op="memory.delete_version")
    assert_own_row(existing, op="memory.delete_version")
    version = version_over(existing)
    # The check this delete owes after its tombstone, recorded first: when the check
    # cannot run now, a retry or the delete_version_check worker runs it, so a write it
    # would have found is never left under the tombstone. A record whose insert failed
    # leaves nothing to run, but one that timed out can still commit: it answers the
    # same 503, and a retry or the worker finishes that delete.
    check = {
        "org": existing.org,
        "namespace": existing.namespace,
        "agent": existing.agent,
        "user_id": existing.user_id,
        "session_id": existing.session_id,
        "memory_key": existing.memory_key,
        "checked": int(input.version),
        "tombstone": version,
    }
    try:
        await memory_repo.record_delete_check(check, done=False)
        await _write_tombstone_row(
            existing, version=version, trace=delete_version_trace(input.version)
        )
        return await finish_delete_check(existing, check)
    except Exception as e:  # a retry finishes it, or the worker once the check is recorded
        raise _check_deferred(e) from e


async def _audit_deferred(input: MemoryDeleteVersionInput) -> None:
    """The op's audit row for a call that answers the deferred 503. The transport writes one
    only for a call that succeeds, and the delete may stand once a retry or the worker runs
    its check: the row names the call as the transport would, its outcome `deferred`. The
    worker that finishes it writes none; a retry is its own call with its own row. Best
    effort, as every audit row is (it goes to the ClickHouse that just failed). Never
    raises."""
    from server.core.logging_config import request_id_var
    from server.observability import audit

    auth = current_auth()
    await audit.emit(
        "memory.delete_version",
        org=input.org,
        namespace=input.namespace,
        actor=auth.subject if auth is not None else "anonymous",
        subject={**audit.input_subject(input), "outcome": "deferred"},
        request_id=request_id_var.get() or "",
    )


class _DeleteDeferred(BackendUnavailableError):
    """The 503 of a delete that did not finish (`_check_deferred`)."""


def _check_deferred(e: Exception) -> _DeleteDeferred:
    """The 503 of a delete that did not finish: its check record, its tombstone, the check
    after it, or a retry's run of an owed check failed. A recorded check stays owed."""
    degradations.note("memory.delete_version", "check_deferred", e)
    log.warning("memory.delete_version: the delete did not finish: %s", e)
    return _DeleteDeferred(
        "the delete did not finish; a retry finishes it, or the background worker once its "
        "check is recorded"
    )


async def finish_delete_check(
    existing: Any, check: dict[str, Any], *, tombstone: int | None = None
) -> MemoryDeleteVersionOutput:
    """The check memory.delete_version owes after its tombstone (the recorded one, or
    `tombstone` when recovery wrote it again), and its answer: when the key moved on
    (`_key_moved_on`) nothing derived is retired; otherwise the delete stands and what
    `existing` (the removed version) derived retires. The recorded check is then marked
    done. Safe to run again: a retry and the delete_version_check worker run it for a
    delete whose own run failed."""
    import server.memory.store as memory_repo

    checked = int(check["checked"])
    tombstone = int(check["tombstone"]) if tombstone is None else tombstone
    moved_on = await _key_moved_on(existing, checked=checked, tombstone=tombstone)
    if moved_on is not None:
        answer = moved_on
    else:
        tombstoned, downgraded = await _retire_derived(
            existing, version=tombstone, op="memory.delete_version", only_its_chunks=True
        )
        answer = MemoryDeleteVersionOutput(
            tombstoned=True,
            version=tombstone,
            dependents_tombstoned=tombstoned,
            dependents_downgraded=downgraded,
        )
        # Here, where every finish of the delete passes (the call itself, a retry of
        # a deferred one, the delete_version_check worker).
        if await webhooks.announce([("memory.tombstoned", memory_change(existing, tombstone))]):
            note_budget_shape("webhook")
    try:
        await memory_repo.record_delete_check(check, done=True)
    except Exception as e:  # noqa: BLE001 — a check left open runs again, harmlessly
        log.warning("memory.delete_version: the check ran but was not marked done: %s", e)
    return answer


async def rerun_delete_check(check: dict[str, Any]) -> MemoryDeleteVersionOutput | None:
    """Run an owed check (the caller holds the key lock) and answer as its delete would
    have, or close it and answer None when the removed version was erased since. When the
    tombstone is missing from the key's history (its insert failed), the check runs against
    another tombstone of this same delete (a retry's), or writes one when nothing came
    after the removed version, at a new version so the namespace's watermark moves, or
    otherwise answers how the key moved on. An insert that timed out can still commit: it
    lands aimed at the same version and counts as this same delete (`_key_moved_on`)."""
    import server.memory.store as memory_repo

    checked, tombstone = int(check["checked"]), int(check["tombstone"])
    ident = (check["agent"], check["user_id"], check["session_id"])
    rows = await memory_repo.fetch_version_rows(
        check["org"],
        check["namespace"],
        [(check["memory_key"], checked), (check["memory_key"], tombstone)],
        sequential=True,
    )
    mine = {r.version: r for r in rows if (r.agent, r.user_id, r.session_id) == ident}
    removed = mine.get(checked)
    if removed is None:
        await memory_repo.record_delete_check(check, done=True)
        return None
    if tombstone not in mine:
        same_delete, others = await _after_checked(removed, checked=checked, tombstone=tombstone)
        if same_delete:
            tombstone = max(same_delete)  # a retry's, or a late one: the delete's that stands
        elif not others:
            # Nothing came after the removed version: the delete is still owed, above any
            # write a fence lifted over a late commit of the original.
            tombstone = _above_lift_band(tombstone)
            await _write_tombstone_row(
                removed, version=tombstone, trace=delete_version_trace(checked)
            )
    return await finish_delete_check(removed, check, tombstone=tombstone)


async def _key_moved_on(
    existing: Any, *, checked: int, tombstone: int
) -> MemoryDeleteVersionOutput | None:
    """The answer when the key moved on after the version check (`checked`), read with
    sequential consistency; None when it did not. Every tombstone of this same delete (the
    split `_after_checked` makes) is set aside, and the newest version left decides. A live
    row means `version_changed`; another remover's tombstone (a delete, a sweep, a dedup or
    a conflict resolution) means it removed the key, not this one (`found=False`). Either
    one below this delete's newest tombstone is written again above it, a tombstone stamped
    as this delete's own so the key's head is the row it removed; a head that already
    carries that row is left. A write still in flight lifts itself when it lands
    (`write_fence.lift_after_insert`)."""
    same_delete, others = await _after_checked(existing, checked=checked, tombstone=tombstone)
    if not others:
        return None
    row = await _version_row(existing, max(others))
    gone = row.tombstoned_at is not None  # another remover took the key
    top = max([tombstone, *same_delete])
    if row.version < top and not (gone and await _head_carries(existing, top, row)):
        # A sweep's marker stays on the sweep's tombstone, which keeps the writes it caught.
        trace = delete_version_trace(checked) if gone else None
        await _write_above(row, tombstone=top, source_trace=trace)
    if gone:
        return MemoryDeleteVersionOutput(tombstoned=False, found=False)
    return MemoryDeleteVersionOutput(tombstoned=False, reason="version_changed")


# What a copy of a tombstone does not carry from it: its own version and write stamps, and
# the trace a copy replaces with this delete's marker.
_NOT_CARRIED = frozenset({"version", "updated_at", "tombstoned_at", "source_trace"})


async def _head_carries(existing: Any, top: int, row: Any) -> bool:
    """Version `top` of the key, this delete's newest tombstone, already carries every
    column of `row` a copy would: a copy this delete wrote back, or its own tombstone of
    the row another remover took. Then it is not copied again. What the removing write
    stamped is not the row's: the `tombstoned` status of a by-id tombstone (a sweep, dedup,
    conflict resolution) and a sweep's marker over the row's own reason."""
    import server.memory.store as memory_repo
    from server.memory.domain.expire_reasons import SWEEP_REASON_PREFIX

    head = await _find_version_row(existing, top)
    if head is None:
        return False
    skip = set(_NOT_CARRIED)
    if row.status == "tombstoned":
        skip.add("status")
    if row.expire_reason.startswith(SWEEP_REASON_PREFIX):
        skip.add("expire_reason")
    mine, theirs = (memory_repo.build_memory_version_row(r) for r in (head, row))
    return all(mine[c] == theirs[c] for c in mine if c not in skip)


async def _restore_newer_chunks(existing: Any, *, tombstone: int) -> None:
    """Put back the chunks the removed row `existing` shares with a value search still
    serves: the key's newest value after it, and every other row of the key in the
    namespace, since a key's memory.set chunk rows are shared by every user, agent and
    session holding it. A chunk row is named by its index and text, so a value keeping a
    passage at the same index shares the row, and its write may land after the delete's
    check but before the retire. A write inserts its row before its chunks, and this read
    follows the retire, so it sees any write whose chunks it caught. The values are read
    again after the restore: a delete, or a write the chunks no longer serve, that found
    them retired and left them has them retired again."""
    from server.memory.app import set_chunks as _set_chunks

    removed = _set_chunks.passage_identities(existing.value, existing.memory_type)
    if not removed:
        return  # a value of no searchable type wrote no chunks
    ch = get_client()
    key_chunks = {
        "org": existing.org,
        "namespace": existing.namespace,
        "memory_key": existing.memory_key,
        "user_id": str(existing.user_id or "-"),
    }
    try:
        shared = removed & await _served_chunks(existing, tombstone=tombstone)
        if not await _set_chunks.restore_set_chunks(identities=shared, ch=ch, **key_chunks):
            return
    except Exception as e:  # noqa: BLE001 — the newer row stands; its chunks only aid search
        log.warning(
            "memory.delete_version: shared chunks of %s not restored: %s", existing.memory_key, e
        )
        return
    try:
        stale = shared - await _served_chunks(existing, tombstone=tombstone)
    except Exception as e:  # noqa: BLE001 — unread, the restored chunks retire again
        log.warning("memory.delete_version: head of %s unread: %s", existing.memory_key, e)
        stale = shared
    if not stale:
        return
    try:
        await _set_chunks.tombstone_set_chunks(only=stale, strong=True, ch=ch, **key_chunks)
    except Exception as e:  # noqa: BLE001 — the delete stands; the chunks only aid search
        log.warning(
            "memory.delete_version: restored chunks of %s not retired again: %s",
            existing.memory_key,
            e,
        )


async def _served_chunks(existing: Any, *, tombstone: int) -> frozenset[tuple[int, str]]:
    """The chunk identities search still serves under the key of `existing`: those of its
    own newest value after it (other than the delete's own `tombstone`), and those of every
    other row of the key in the namespace that search serves. A row counts when it is
    embedded too (a high row's chunks stay retired)."""
    import server.memory.store as memory_repo
    from server.memory.app import set_chunks as _set_chunks

    served: set[tuple[int, str]] = set()
    for r in await memory_repo.served_key_rows(
        existing.org,
        existing.namespace,
        existing.memory_key,
        besides=(existing.agent, existing.user_id, existing.session_id),
    ):
        value = memory_repo.parse_memory_value(str(r["value"]))
        served |= _set_chunks.set_chunk_identities(
            value, str(r["memory_type"]), existing.memory_key
        )
    return frozenset(served | await _own_newer_chunks(existing, tombstone=tombstone))


async def _own_newer_chunks(existing: Any, *, tombstone: int) -> frozenset[tuple[int, str]]:
    """The chunk identities of the key's newest value after `existing` under its own
    identity (other than the delete's own `tombstone`) when search serves it."""
    from server.memory.app import set_chunks as _set_chunks

    same_delete, others = await _after_checked(
        existing, checked=int(existing.version), tombstone=tombstone
    )
    if not others or max(same_delete, default=0) > max(others):
        return frozenset()  # nothing after it, or this delete's own tombstone on top
    newer = await _version_row(existing, max(others))
    if newer.tombstoned_at is not None:
        return frozenset()
    if not is_embedded(newer.memory_type, newer.sensitivity):
        return frozenset()
    return _set_chunks.set_chunk_identities(newer.value, newer.memory_type, newer.memory_key)


async def _after_checked(
    existing: Any, *, checked: int, tombstone: int
) -> tuple[list[int], list[int]]:
    """The key's committed versions above `checked` under the identity of `existing`, read
    with sequential consistency, other than the delete's own `tombstone`: the other
    tombstones of this same delete (aimed at `checked`: a late or retried insert of it, or
    a copy it wrote back), and the rest."""
    import server.memory.store as memory_repo

    key = {
        "agent": existing.agent,
        "user_id": existing.user_id,
        "session_id": existing.session_id,
        "memory_key": existing.memory_key,
        "version": checked,
    }
    committed = await memory_repo.versions_above(existing.org, existing.namespace, [key])
    above = committed.get(
        (existing.agent, existing.user_id, existing.session_id, existing.memory_key), []
    )
    same_delete: list[int] = []
    others: list[int] = []
    for v, deleted, target in above:
        if v != tombstone:
            (same_delete if deleted and target == checked else others).append(v)
    return same_delete, others


async def _find_version_row(existing: Any, version: int) -> Any | None:
    """Version `version` of the key under the identity of `existing`, read with sequential
    consistency; None when it is not committed."""
    import server.memory.store as memory_repo

    ident = (existing.agent, existing.user_id, existing.session_id)
    rows = await memory_repo.fetch_version_rows(
        existing.org, existing.namespace, [(existing.memory_key, version)], sequential=True
    )
    return next((r for r in rows if (r.agent, r.user_id, r.session_id) == ident), None)


async def _version_row(existing: Any, version: int) -> Any:
    """`_find_version_row` for a version a listing just saw committed."""
    row = await _find_version_row(existing, version)
    if row is None:
        raise RuntimeError(f"version {version} of {existing.memory_key!r} was listed, not read")
    return row


def _above_lift_band(tombstone: int) -> int:
    """A version drawn on this process's clock, but above the band the write fence lifts
    writes into over `tombstone` (`write_fence.LIFT_BAND_NS`), so it cannot tie with one
    when another process's clock put the tombstone ahead of this one."""
    from server.memory.app import write_fence

    return max(next_version(), tombstone + write_fence.LIFT_BAND_NS + 1)


async def _write_above(row: Any, *, tombstone: int, source_trace: str | None = None) -> None:
    """`row` written again above `tombstone` (`_above_lift_band`), `source_trace` replacing
    its own when given."""
    import server.memory.store as memory_repo

    changes = {} if source_trace is None else {"source_trace": source_trace}
    back = memory_repo.build_memory_version_row(
        row,
        version=_above_lift_band(tombstone),
        updated_at=memory_repo.written_now(),
        **changes,
    )
    await insert_memory_versions([back], ch=get_client())


async def _write_tombstone_row(existing: Any, *, version: int, trace: str = "") -> None:
    """The loom.memory tombstone of `existing` at `version`, `trace` as its source_trace."""
    import server.memory.store as memory_repo
    from server.observability.otel import traced

    now = datetime.now(UTC)
    # build_memory_version_row carries every live column forward (avoiding
    # silent column loss); only the tombstone deltas override.
    tombstone_row = memory_repo.build_memory_version_row(
        existing,
        version=version,
        updated_at=memory_repo.written_now(),
        tombstoned_at=now,
        source_trace=trace,
        expires_at=None,
    )
    with traced("ch.insert.memory.tombstone", table="loom.memory"):
        # Over the head it read: a lock taken since is never dropped by the tombstone.
        await memory_repo.insert_versions_over_heads([tombstone_row], [existing], ch=get_client())


async def _retire_derived(
    existing: Any, *, version: int, op: str, only_its_chunks: bool = False
) -> tuple[int, int]:
    """Retire what was derived from the row a tombstone at `version` removed: its vectors,
    its chunks, a procedure's run ledger, a decision's record, and the summaries built from
    it. Returns how many consolidated summaries it tombstoned and downgraded."""
    import server.memory.store as memory_repo

    ch = get_client()
    now = datetime.now(UTC)

    # Propagate the tombstone to memory_embeddings — search reads that table, so without it
    # soft-deleted memories keep surfacing. Each live row of the key (one per scope and
    # embedding model) is re-emitted with every column and its vectors carried, so it
    # collapses onto the original under the RMT sort key. Every memory type takes this path:
    # the set paths skip the row for a non-searchable type, but the table can hold one anyway
    # (dim-0 stubs, rows other paths wrote). A key with no live row writes nothing.
    try:
        await memory_repo.tombstone_embeddings_for_key(
            existing.org,
            existing.namespace,
            existing.agent,  # the row's own identity, which may differ from the caller's
            existing.user_id,
            existing.session_id,
            existing.memory_key,
            ts=now.replace(tzinfo=None) if now.tzinfo else now,
            version=version,
            ch=ch,
        )
    except Exception as e:  # noqa: BLE001 — the memory row tombstone already landed; mirror is best-effort
        log.warning("memory_embeddings tombstone write failed (non-fatal): %s", error_summary(e))

    # The verbatim chunks a long value wrote under this key go with it, or search keeps
    # lifting a deleted fact through its own body.
    if is_searchable_type(existing.memory_type):
        from server.memory.app import set_chunks as _set_chunks

        # memory.delete_version removes one version: a newer value's chunks stay.
        await _set_chunks.tombstone_row_chunks_fail_open(
            existing, ch=ch, only_its_own=only_its_chunks
        )
        if only_its_chunks:
            await _restore_newer_chunks(existing, tombstone=version)

    # A procedure's run ledger must not outlive the procedure.
    # Best-effort: the tombstone above is the erasure of record; a ledger
    # failure is logged, not raised, and namespace erasure sweeps it anyway.
    if existing.memory_type == "procedural":
        try:
            await memory_repo.delete_procedure_runs_by_key(
                existing.org, existing.namespace, existing.memory_key
            )
        except Exception as e:  # noqa: BLE001 — the tombstone stands
            log.warning(
                "%s: procedure_runs cascade failed for %s: %s",
                op,
                existing.memory_key,
                error_summary(e),
            )
    # A decision's record is retired once its writer's row is gone, or decision.chain and
    # decision.precedents keep serving the text the writer deleted; under Postgres the
    # tombstone's publication retires it (`pg_publish.retire_decision_records`).
    if existing.memory_type == "decision" and not pg.enabled():
        try:
            await memory_repo.tombstone_decision_records(
                existing.org, existing.namespace, [existing.memory_key], version=version, ch=ch
            )
        except Exception as e:  # noqa: BLE001 — the tombstone stands
            log.warning(
                "%s: decision record not retired for %s: %s",
                op,
                existing.memory_key,
                error_summary(e),
            )
    # A summary built from this row now says something its evidence no
    # longer supports. If none of its sources survive it goes too; otherwise
    # it is downgraded to `conflicted` (review queue) with a
    # `stale-source:<key>` tag. Best effort — the delete never fails on it.
    dep_tomb = dep_down = 0
    try:
        dep_tomb, dep_down = await memory_repo.downgrade_consolidated_dependents(
            existing.org,
            existing.namespace,
            existing.memory_key,
            ch=ch,
        )
    except Exception as e:  # noqa: BLE001 — the tombstone stands
        log.warning(
            "%s: dependent summaries not downgraded for %s: %s",
            op,
            existing.memory_key,
            error_summary(e),
        )
    return dep_tomb, dep_down


async def handle_memory_clear(input: MemoryClearInput) -> MemoryClearOutput:
    """Bulk-tombstone for `memory.clear`.

    Defenses: a concrete `user_id` or `agent` other than `'-'` is required (`"*"` opens
    an axis; a scope-less clear is `erasure.request_namespace`'s job), and
    `confirm_subject` must match one (guard against UI-dropdown accidents).

    Same tombstone semantics as `memory.delete`.
    """
    import server.memory.store as memory_repo

    # "*" opens an axis as it does on reads; matched literally it would clear nothing.
    user_id = None if input.user_id == "*" else input.user_id
    agent = None if input.agent == "*" else input.agent
    # One axis must name someone: "-" (no user, no agent) beside an open axis selects most
    # of a namespace, which is erasure's job too, and anyone can type it to confirm.
    named = [v for v in (user_id, agent) if v not in (None, "-")]
    if not named:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_SCOPE",
                "message": (
                    "memory.clear requires a concrete `user_id` or `agent` other than '-' "
                    '("*" means every value of that field). For namespace-wide clearing use '
                    "erasure.request_namespace (full saga, cross-system "
                    "cascade, auditable)."
                ),
            },
        )
    if input.confirm_subject not in named:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRM_SUBJECT_MISMATCH",
                "message": (
                    "`confirm_subject` must equal the concrete user_id or agent "
                    'being cleared ("*" and "-" never confirm). This is a mid-weight '
                    "defense against destructive UI-dropdown accidents."
                ),
            },
        )

    async def announce_page(rows: list[dict[str, Any]]) -> None:
        await webhooks.announce([("memory.tombstoned", row) for row in rows])

    # The tombstoned rows are read back only for a namespace with a webhook that takes them.
    subscribed = await webhooks.subscribers(input.org, input.namespace, "memory.tombstoned")
    counts = await memory_repo.bulk_tombstone_scope(
        org=input.org,
        namespace=input.namespace,
        user_id=user_id,
        agent=agent,
        on_tombstoned=announce_page if subscribed else None,
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
    touched: list[Any] = []  # the head each touch row is built from
    versions: dict[str, int] = {}
    for key in wanted:
        existing = by_key.get(key)
        if existing is None:
            continue
        version = version_over(existing)
        versions[key] = version
        # Mirror every persisted column; only the touch deltas override.
        overrides: dict[str, Any] = {"version": version, "expires_at": new_expires_at}
        if input.on_expire:
            overrides["on_expire"] = input.on_expire
        if input.expire_reason:
            overrides["expire_reason"] = input.expire_reason[:512]
        touch_rows.append(memory_repo.build_memory_version_row(existing, **overrides))
        touched.append(existing)
    if touch_rows:
        await memory_repo.insert_versions_over_heads(touch_rows, touched, ch=ch)
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
            # a 'high' head keeps no vector: never copy one forward under it
            if is_embedded(r.memory_type, r.sensitivity)
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
    from server.memory.app.id_namespace import namespace_for_id

    # Addressed by an org-wide id: the transport had no namespace to bind, so a
    # namespace the caller cannot read is hidden, not refused.
    namespace = await namespace_for_id(input.org, input.memory_id, strong=False)
    if namespace is None:
        return MemoryGetByIdOutput(found=False)
    row = await memory_repo.get_by_id(
        input.memory_id,
        org=input.org,
        namespace=namespace,
        as_of=input.as_of,
        include_usage_totals=True,
        fresh=True,
        scope=request_scope(),
    )
    if row is None:
        return MemoryGetByIdOutput(found=False)
    # A 'high' row past its retention window is never served, as memory.bulk_get leaves it out.
    expires_at = row.expires_at
    if (
        row.sensitivity == "high"
        and expires_at
        and expires_at.replace(tzinfo=None) <= naive_utc_now()
    ):
        return MemoryGetByIdOutput(found=False)
    high = str(getattr(row, "sensitivity", "") or "") == "high"
    # A caller that only names the row (the dashboard's conflict peer) never reads a
    # high value, so nothing sensitive was read and nothing is audited.
    omit_value = high and input.omit_high_value
    sha = served_content_sha256(row.value)
    if high and not omit_value:
        # Key-only read of a 'high' row: audited. Never raises.
        from server.observability import audit as _audit

        note_budget_shape("audited")
        await _audit.emit(
            "memory.sensitive_read",
            org=input.org,
            namespace=row.namespace,
            # The reader, from the token; get_by_id names no agent to fall back on.
            actor=writer_actor("-"),
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
        value=None if omit_value else row.value,
        confidence=row.confidence,
        version=row.version,
        created_at=row.created_at,
        expires_at=row.expires_at,
        on_expire=getattr(row, "on_expire", "") or "",
        expire_reason=getattr(row, "expire_reason", "") or "",
        valid_at=getattr(row, "valid_at", None),
        valid_to=getattr(row, "valid_to", None),
        pinned=row.pinned,
        # The hash of a short secret names it as well as the value does.
        content_sha256="" if omit_value else sha,
        verified_by=getattr(row, "verified_by", "") or "",
        actor=getattr(row, "actor", "") or "",
        source_class=getattr(row, "source_class", "") or "",
        **_lock.lock_state(row),
        verified_at=getattr(row, "verified_at", None),
        # From the stored hash, also when the answer omits it.
        verification=_trust.verification_state(
            getattr(row, "verified_by", ""), getattr(row, "verified_sha256", ""), sha
        ),
    )
