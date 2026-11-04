"""memory.update and memory.update_by_id: revise a live row in place under
its identity, the preconditions a revision checks, and the embedding row it
re-emits."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from server.code.app.member_writes import assert_own_row
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import served_content_sha256
from server.core.hashing import sha256_value as _content_sha256
from server.core.naming import named
from server.core.vectors import codes_state
from server.embedding import get_embedder
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import (
    MemoryUpdateByIdInput,
    MemoryUpdateByIdOutput,
    MemoryUpdateCandidate,
    MemoryUpdateInput,
    MemoryUpdateOutput,
)
from server.memory.app import after_write, webhooks
from server.memory.app import provenance as app_provenance
from server.memory.app.auto_resolution import Follow, follow_beside_response
from server.memory.app.conflicts import same_key_mark
from server.memory.app.edge_links import (
    drop_undeclared_edges_beside,
    owes_undeclared_drop,
    project_revised_edges,
)
from server.memory.app.high_fence import fence_high
from server.memory.app.projection_errors import ProjectionScope
from server.memory.app.set_entities import declared_entities
from server.memory.app.triple_projections import project_stated_relations
from server.memory.domain import lock as _lock
from server.memory.domain.embed_text import _build_embed_text, key_axis_text
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.memory_types import is_embedded
from server.memory.store import key_write_lock
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version
from server.observability.budget_shape import note_budget_shape

if TYPE_CHECKING:
    from server.memory.store import MemoryRow


async def _embed_value_and_key(
    embedder: Any, embed_text: str, key_text: str
) -> tuple[list[float], list[float]]:
    """The value and key vectors of one write in one round trip: `embed_many` when the
    embedder has it (one API call, as memory.set), else the two calls concurrently. No key
    text, no key vector."""
    if not key_text:
        return await embedder.embed(embed_text), []
    embed_many = getattr(embedder, "embed_many", None)
    if embed_many is not None:
        vectors = await embed_many([embed_text, key_text])
        if len(vectors) == 2:
            return vectors[0], vectors[1]
    value_vec, key_vec = await asyncio.gather(embedder.embed(embed_text), embedder.embed(key_text))
    return value_vec, key_vec


def _pinned_conflict(op: str, existing: Any, conflict_result: Any) -> HTTPException:
    """The 409 a revision gets when its new value contradicts a pinned peer."""
    return HTTPException(
        status_code=409,
        detail={
            "code": "PINNED_CONFLICT",
            "message": (
                f"{op}: the new value for {existing.memory_key!r} conflicts "
                f"with a pinned memory ({conflict_result.pinned_blocker_id!r}); "
                "update the pinned row explicitly or unpin it first"
            ),
            "pinned_blocker_id": conflict_result.pinned_blocker_id,
            "peer_ids": [p.memory_id for p in conflict_result.peers],
            "memory_key": existing.memory_key,
            "memory_id": str(existing.memory_id),
        },
    )


def _cf_start_write_config(org: str, existing: Any, checked: bool) -> Any:
    """The write-config prefetch for a revision; a resolved empty config when the row takes no
    conflict check (every field None: the readers fall back to their own reads)."""
    from server.memory.app import conflicts as _cf
    from server.memory.domain import conflicts as conflict_rules

    if not checked:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        fut.set_result(conflict_rules.WriteConfig())
        return fut
    return _cf.start_write_config(
        org, existing.namespace, existing.memory_type, existing.memory_key
    )


async def _revision_supersession(
    org: str,
    existing: Any,
    serialized_value: str,
    ch: Any,
    policy: str | None,
    follows: list[Follow],
) -> None:
    """Same-key supersession for a revision; `policy` None reads it."""
    from server.memory.app.key_supersession import supersede_same_key_rows

    batch = [
        {
            "memory_id": str(existing.memory_id),
            "memory_key": existing.memory_key,
            "value": serialized_value,
        }
    ]
    await supersede_same_key_rows(
        org=org,
        namespace=existing.namespace,
        new_rows=batch,
        ch=ch,
        follows=follows,
        policy=policy,
        agent=str(existing.agent),
    )


async def handle_memory_update_by_id(input: MemoryUpdateByIdInput) -> MemoryUpdateByIdOutput:
    """Update a memory's `value` by memory_id. Re-uses the existing
    memory_key + identity tuple; writes a new ReplacingMergeTree row
    with a bumped version so FINAL collapses to the updated content.

    Embedding row is also rewritten when the memory_type is searchable,
    so memory.search picks up the new content. Returns updated=False
    when memory_id doesn't resolve to a live row.
    """
    import server.memory.store as memory_repo

    ch = get_client()
    existing = await memory_repo.get_by_id(
        input.memory_id,
        org=input.org,
        fresh=True,
        # With a hash precondition the read must be of the latest version.
        strong=True,  # always — the head feeds a write
        scope=WHOLE_NAMESPACE,
    )
    if existing is None:
        return MemoryUpdateByIdOutput(updated=False)
    # A write by org-wide id: WRITER on the row's namespace, checked here
    # because the transport had no namespace to bind.
    from server.auth.authz import row_in_scope
    from server.operations.base import Role as _Role

    if not await row_in_scope(input.org, existing.namespace, _Role.WRITER):
        return MemoryUpdateByIdOutput(updated=False)
    return await _update_live_row(input, existing, ch, op="memory.update_by_id")


async def handle_memory_update(input: MemoryUpdateInput) -> MemoryUpdateOutput:
    """Update a memory addressed by (namespace, memory_key) — the operation
    for "the fact changed or got more specific".

    The key is resolved namespace-wide unless the caller narrows it with
    agent / user_id / session_id: a caller who just read the key through
    search or ask holds the key, not the identity tuple the writer used.
    One live row → the same code path as update_by_id. No row → updated=False,
    reason="not_found". Several rows → 409 AMBIGUOUS_KEY listing them.
    """
    import server.memory.store as memory_repo

    ch = get_client()
    candidates = await _live_rows_for_key(input, ch)
    if not candidates:
        return MemoryUpdateOutput(updated=False, memory_key=input.memory_key, reason="not_found")
    if len(candidates) > 1:
        rows = [
            MemoryUpdateCandidate(
                memory_id=str(r["memory_id"]),
                agent=str(r.get("agent") or ""),
                user_id=str(r.get("user_id") or ""),
                session_id=str(r.get("session_id") or ""),
                version=int(r.get("version") or 0),
                status=str(r.get("status") or ""),
            )
            for r in candidates
        ]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AMBIGUOUS_KEY",
                "message": (
                    f"memory_key {input.memory_key!r} is live under "
                    f"{len(rows)} identity tuples in namespace "
                    f"{input.namespace!r}; pass a named agent/user_id/session_id "
                    'to narrow it ("-" does not narrow, nor "*" for agent), or '
                    "memory.update_by_id with one of the ids"
                ),
                "memory_key": input.memory_key,
                "candidates": [c.model_dump() for c in rows],
            },
        )
    memory_id = str(candidates[0]["memory_id"])
    existing = await memory_repo.get_by_id(
        memory_id,
        org=input.org,
        fresh=True,
        strong=True,  # always — the head feeds a write
        scope=WHOLE_NAMESPACE,
    )
    if existing is None:
        return MemoryUpdateOutput(updated=False, memory_key=input.memory_key, reason="not_found")
    assert_own_row(existing, op="memory.update")
    out = await _update_live_row(input, existing, ch, op="memory.update")
    return MemoryUpdateOutput(
        updated=out.updated,
        memory_id=memory_id,
        memory_key=existing.memory_key,
        new_version=out.new_version,
        content_sha256=out.content_sha256,
        conflict_verdict=out.conflict_verdict,
        conflict_peers=out.conflict_peers,
        duplicate_of=out.duplicate_of,
    )


async def _live_rows_for_key(input: MemoryUpdateInput, ch: Any) -> list[dict[str, Any]]:
    """Live (not tombstoned, not superseded) rows carrying `memory_key`
    under the namespace, newest version first, narrowed by whichever
    identity fields the caller supplied. Expired rows are included: the
    update path answers 409 EXPIRED with a revive hint for them."""
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "memory_key = {key:String}",
        "tombstoned_at IS NULL",
        "status IN ('active', 'duplicate', 'conflicted')",
    ]
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "key": input.memory_key,
    }
    # "-" (the identity sentinel the SDKs stamp) means unset, and "*" too for agent,
    # as in memory.ask.
    narrowing = {
        "agent": named(input.agent, wildcard=True),
        "user_id": named(input.user_id),
        "session_id": named(input.session_id),
    }
    for col, val in narrowing.items():
        if val is not None:
            where.append(f"{col} = {{{col}:String}}")
            params[col] = val
    sql = f"""
        SELECT memory_id, agent, user_id, session_id, version, status
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY version DESC
        LIMIT 10
        """
    rows = await ch.query_all(sql, params=params)
    if not rows:
        # A key written moments ago can be missing on a lagging replica, which would answer
        # 200 updated:false. One sequentially consistent retry on a miss.
        from server.memory.store import _SEQUENTIAL, read_your_writes_enabled

        if read_your_writes_enabled():
            rows = await ch.query_all(sql.rstrip() + "\n        " + _SEQUENTIAL, params=params)
    return list(rows or [])


def _update_embedding_row(
    existing: Any,
    memory_row: dict[str, Any],
    embedder: Any,
    embedding: list[float],
    key_embedding: list[float],
    *,
    has_key: bool,
    conflict_checked_at: Any = None,
) -> dict[str, Any]:
    """The loom.memory_embeddings row mirroring an update's version row `memory_row`.

    Each column the version row decides (value, run, confidence, tags, concepts, status,
    provenance, attestation) is read from it; the rest are carried from `existing`, else
    FINAL surfaces zeroed columns over the prior real values."""
    import server.memory.store as memory_repo
    from server.memory.domain.trust import CONFIRMED_STATES, verification_state

    return memory_repo.build_embedding_row(
        org=memory_row["org"],
        namespace=existing.namespace,
        agent=existing.agent,
        user_id=existing.user_id,
        session_id=existing.session_id,
        run_id=memory_row["run_id"],
        memory_key=existing.memory_key,
        memory_type=existing.memory_type,
        scope="private",
        content_excerpt=build_excerpt(memory_row["value"], memory_key=existing.memory_key),
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        embedding=embedding,
        memory_key_embedding=key_embedding,
        memory_key_embedding_dim=embedder.dim if has_key else 0,
        confidence=float(memory_row["confidence"]),
        tombstoned_at=None,
        created_at=naive_utc_now(),
        valid_at=getattr(existing, "valid_at", None),
        valid_to=getattr(existing, "valid_to", None),
        expires_at=getattr(existing, "expires_at", None),
        tags=memory_row["tags"],
        entities=list(getattr(existing, "entities", []) or []),
        files=list(getattr(existing, "files", None) or []),
        concepts=memory_row["concepts"],
        temporal_anchor=getattr(existing, "temporal_anchor", None),
        temporal_confidence=getattr(existing, "temporal_confidence", 0.0),
        reinforcement_count=getattr(existing, "reinforcement_count", 1),
        last_reinforced_at=getattr(existing, "last_reinforced_at", None),
        source_resource_id=getattr(existing, "source_resource_id", "") or "",
        mentioned_at=getattr(existing, "mentioned_at", None),
        invalid_at=getattr(existing, "invalid_at", None),
        # The version row's status (a verdict's, else the row's own: a conflicted row edited
        # without a check stays conflicted in search), its provenance class (memory.ask's
        # unverified-external filter and the trust term read it here) and whether its
        # attestation still binds the new hash.
        status=memory_row["status"],
        source_class=memory_row["source_class"] or "",
        # The version row's: a newer write's retire or fence outranks a late vector.
        version=int(memory_row["version"]),
        verified=int(
            verification_state(
                memory_row["verified_by"],
                memory_row["verified_sha256"],
                memory_row["content_sha256"],
            )
            in CONFIRMED_STATES
        ),
        # The conflict resolver's owed mark when the check asks it to look.
        conflict_checked_at=conflict_checked_at,
        content_sha256=str(memory_row["content_sha256"] or ""),
    )


def _refuse_blind_revival(input: Any, existing: MemoryRow) -> None:
    """An expired fact is not edited by accident: the read filter already hides it from
    get/search, so a blind update would revive it silently. `revive=True` says so out
    loud and clears the expiry."""
    _expired_at = getattr(existing, "expires_at", None)
    if _expired_at is None or _expired_at > naive_utc_now():
        return
    if not input.revive:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXPIRED",
                "message": (
                    f"memory {existing.memory_key!r} expired at "
                    f"{_expired_at.isoformat()}Z (on_expire="
                    f"{getattr(existing, 'on_expire', '') or 'tombstone'}); pass "
                    "revive=true to update it and clear the expiry, or leave it "
                    "to the expiry sweep"
                ),
                "memory_key": existing.memory_key,
                "memory_id": str(existing.memory_id),
                "expires_at": _expired_at.isoformat(),
                "on_expire": getattr(existing, "on_expire", "") or "",
            },
        )


def _check_update_precondition(input: Any, existing: MemoryRow) -> None:
    """Optimistic concurrency: the live row's hash must match before accepting."""
    if not input.if_match_sha256:
        return
    current_sha = served_content_sha256(existing.value)
    if current_sha != input.if_match_sha256:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRECONDITION_FAILED",
                "message": (
                    f"if_match_sha256 does not match current row (current_sha256={current_sha!r})"
                ),
                "current_sha256": current_sha,
            },
        )


async def _revised_edges(
    existing: MemoryRow,
    memory_row: dict[str, Any],
    new_value: Any,
    new_sha: str,
    sensitivity: str,
    *,
    op: str,
    ch: Any,
) -> None:
    """The edges of a revision's new value; a 'high' row keeps only its declared
    entities' edges, so what any value's text gave the graph goes, after the response."""
    if new_sha != existing.content_sha256:
        await project_revised_edges(memory_row, new_value, op=op, ch=ch)
    declared = declared_entities(memory_row.get("entities"))
    if sensitivity == "high" and owes_undeclared_drop(existing, declared):
        scope = ProjectionScope(
            org=existing.org,
            namespace=existing.namespace,
            agent=str(existing.agent),
            user_id=str(existing.user_id),
        )
        memory_id = str(existing.memory_id)
        await drop_undeclared_edges_beside(scope, {memory_id: int(memory_row["version"])}, op=op)


def _update_sensitivity(
    existing: MemoryRow, serialized_value: str, tags: list[str]
) -> tuple[str, list[str]]:
    """(the revised row's sensitivity, its tags with the `sensitivity:` tag): the
    level memory.set would give the new value (autodetected when the setting is on),
    never below the row's own label."""
    from server.memory.domain.sensitivity import (
        detector_text,
        max_sensitivity,
        resolve_sensitivity,
        sensitivity_tags,
    )

    level = max_sensitivity(
        [
            getattr(existing, "sensitivity", ""),
            resolve_sensitivity("", detector_text(serialized_value)),
        ]
    )
    return level, [*tags, *sensitivity_tags(level, tags)]


def _revised_fields(input: Any, existing: MemoryRow) -> tuple[Any, str, str, list[str], list[str]]:
    """The revision's value (serialized and hashed), tags and concepts; a field
    the input leaves None keeps the row's own."""
    # A tags/concepts-only rewrite keeps the value; the row is still
    # re-emitted whole (never an in-place ALTER).
    new_value = existing.value if input.value is None else input.value
    serialized_value = json.dumps(new_value)
    new_sha = _content_sha256(new_value)
    new_tags = (
        list(existing.tags or [])
        if input.tags is None
        else [str(t).strip().lower() for t in input.tags if str(t).strip()]
    )
    new_concepts = (
        list(getattr(existing, "concepts", None) or [])
        if input.concepts is None
        else [str(c).strip() for c in input.concepts if str(c).strip()]
    )
    return new_value, serialized_value, new_sha, new_tags, new_concepts


def _enforce_char_limit(char_limit: int | None, serialized_value: str) -> None:
    # Server-enforced char cap (None = no cap). Callers preserving an
    # existing cap read the row's char_limit first and pass it back.
    if char_limit is not None and len(serialized_value) > char_limit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CHAR_LIMIT_EXCEEDED",
                "message": (
                    f"value length {len(serialized_value)} exceeds char_limit {char_limit}"
                ),
                "value_length": len(serialized_value),
                "char_limit": char_limit,
            },
        )


async def _update_conflict_check(
    input: Any,
    existing: MemoryRow,
    *,
    serialized_value: str,
    embedding: list[float],
    cfg_task: Any,
    op: str,
) -> Any:
    """The semantic conflict check memory.set runs, on the revised value; a
    pinned contradiction raises before the version is written."""
    from server.memory.app.conflicts import check_on_commit
    from server.observability.otel import traced

    with traced("conflicts.check_on_commit", memory_type=existing.memory_type, op=op):
        conflict_result = await check_on_commit(
            org=input.org,
            namespace=existing.namespace,
            agent=existing.agent,
            memory_type=existing.memory_type,
            memory_key=existing.memory_key,
            new_value=serialized_value,
            new_embedding=embedding,
            **(await cfg_task).check_kwargs(),
        )
    if conflict_result.verdict == "rejected":
        raise _pinned_conflict(op, existing, conflict_result)
    return conflict_result


def _status_columns(conflict_result: Any) -> tuple[str, dict[str, Any]]:
    """The duplicate_of id and the status columns the verdict gives the new
    version.

    A checked value that came back clear (or refines) resets a stale
    `conflicted` / `duplicate` mark from the OLD value: the peers it was
    flagged against were about text this version no longer holds. A
    restatement of another row (`merge`) is written as a duplicate of it — the
    caller addressed this row on purpose, so unlike memory.set the write is
    not skipped."""
    duplicate_of_id = ""
    status_cols: dict[str, Any] = {}
    if conflict_result is not None:
        if conflict_result.verdict == "duplicate":
            duplicate_of_id = conflict_result.duplicate_of_id or ""
        elif conflict_result.verdict == "merge":
            duplicate_of_id = conflict_result.existing_memory_id or ""
        if duplicate_of_id:
            status_cols = {"status": "duplicate", "duplicate_of": duplicate_of_id}
        elif conflict_result.verdict in ("clear", "refines", "conflict"):
            # `conflict` is stamped after the write by mark_peers_conflicted,
            # exactly as memory.set does; start from a clean row.
            status_cols = {"status": "active", "duplicate_of": "", "conflict_with": []}
    return duplicate_of_id, status_cols


def _revision_run_id(input: Any, existing: MemoryRow) -> str:
    """A revision made under a run carries that run on its version row (the `acted` fold joins
    the run's writes to what the run was shown); a caller naming no run keeps the row's own."""
    revision_run_id = named(str(getattr(input, "run_id", "") or "").strip())
    return revision_run_id or str(getattr(existing, "run_id", "-") or "-")


async def _update_post_write(
    input: Any,
    existing: MemoryRow,
    conflict_result: Any,
    ch: Any,
    follows: list[Follow],
) -> Any:
    """Post-write, as memory.set: a conflict flags both sides (the new version
    exists, so the cross-link resolves); a templated substitution supersedes the
    rows it replaces. Returns the verdict that landed: `clear`
    when the mark flagged nothing (no peer, or the new row, was left live)."""
    from server.observability.otel import traced

    if conflict_result is not None and conflict_result.verdict == "conflict":
        from server.memory.app.conflicts import mark_peers_conflicted

        with traced("conflicts.mark_peers", peers=len(conflict_result.peers)):
            landed = await mark_peers_conflicted(
                org=input.org,
                new_memory_id=str(existing.memory_id),
                peer_ids=[p.memory_id for p in conflict_result.peers],
            )
        if landed != "conflict":
            # Every peer, or the new row itself, retired before the mark:
            # nothing was flagged.
            conflict_result = replace(conflict_result, verdict="clear", peers=[])
    elif (
        conflict_result is not None
        and conflict_result.verdict == "refines"
        and conflict_result.refined_peers
    ):
        from server.memory.app.conflicts import apply_refines

        with traced("conflicts.apply_refines", peers=len(conflict_result.refined_peers)):
            await apply_refines(
                org=input.org,
                namespace=existing.namespace,
                new_memory_id=str(existing.memory_id),
                new_memory_key=existing.memory_key,
                refined_peers=conflict_result.refined_peers,
                ch=ch,
                follows=follows,
            )
    return conflict_result


async def _update_live_row(
    input: Any, existing: MemoryRow, ch: Any, *, op: str
) -> MemoryUpdateByIdOutput:
    """A conditional update re-reads the head under the key's write lock."""
    if not input.if_match_sha256:
        return await _write_revision(input, existing, ch, op=op)
    import server.memory.store as memory_repo

    async with key_write_lock.key_write_locks.hold(
        input.org,
        existing.namespace,
        existing.agent,
        existing.user_id,
        existing.session_id,
        existing.memory_key,
    ):
        head = await memory_repo.get_by_id(
            str(existing.memory_id), org=input.org, fresh=True, strong=True, scope=WHOLE_NAMESPACE
        )
        if head is None:
            return MemoryUpdateByIdOutput(updated=False)
        return await _write_revision(input, head, ch, op=op)


# Its own function: _write_revision is at the function-length gate's 50 statements.
async def _record_revision(memory_row: dict[str, Any], *, op: str, verdict: str) -> None:
    """The version row is in: its one hash-only audit row, then the `memory.revised`
    event, noted as the "webhook" budget shape when it sent outbox rows."""
    from server.core.logging_config import request_id_var
    from server.observability import audit

    await audit.emit(
        event_type=op,
        org=memory_row["org"],
        namespace=memory_row["namespace"],
        actor=memory_row["actor"],
        # Hashes and ids, never the value; the verdict the write was checked with.
        subject={
            "memory_key": memory_row["memory_key"],
            "memory_id": str(memory_row["memory_id"]),
            "content_sha256": memory_row["content_sha256"],
            "source_class": memory_row["source_class"],
            "version": memory_row["version"],
            "verdict": verdict,
        },
        request_id=request_id_var.get() or "",
    )
    if await webhooks.announce([("memory.revised", memory_row)]):
        note_budget_shape("webhook")


async def _write_revision(
    input: Any, existing: MemoryRow, ch: Any, *, op: str
) -> MemoryUpdateByIdOutput:
    """The write half shared by memory.update_by_id and memory.update:
    lock, expiry/revive, hash precondition, char cap, blacklist, one new
    version row, embedding refresh. `input` carries value / tags / concepts /
    if_match_sha256 / char_limit / revive (and, for memory.update, confidence)."""
    import server.memory.store as memory_repo

    _lock.assert_unlocked(existing, "value", op=op)
    _refuse_blind_revival(input, existing)
    _check_update_precondition(input, existing)

    new_version = next_version()
    new_confidence = getattr(input, "confidence", None)
    new_value, serialized_value, new_sha, new_tags, new_concepts = _revised_fields(input, existing)
    effective_char_limit = input.char_limit
    _enforce_char_limit(effective_char_limit, serialized_value)

    # Blacklist enforcement ("Refuse") — parity with memory.set, else an
    # edit could smuggle a blacklisted value past the write-time gate.
    from server.memory.app.blacklist_guard import enforce_on_insert

    await enforce_on_insert(
        org=input.org,
        namespace=existing.namespace,
        value=serialized_value,
    )

    # The new value meets the namespace the way a memory.set value does — same semantic
    # conflict check, same verdicts — and it runs BEFORE the version is written so a pinned
    # contradiction never lands. check_on_commit excludes the row's own key, so the row's
    # earlier versions are never their own peer. Skipped for non-searchable types (no
    # embedding to compare) and when the flag is off.
    # The new value is labelled as memory.set labels one, never below the row's own
    # label: a value that now holds a high shape is 'high' and is not embedded.
    sensitivity, new_tags = _update_sensitivity(existing, serialized_value, new_tags)
    searchable = is_embedded(existing.memory_type, sensitivity)
    embedder = get_embedder() if searchable else None
    if embedder is not None:
        # Before the version row lands beside an embedding the coded table refuses.
        await codes_state.refuse_width("memory_embeddings", embedder.dim)
    # The namespace's write config in one round trip beside the embedding call, as memory.set
    # does — not six reads in a row.
    cfg_task = _cf_start_write_config(input.org, existing, embedder is not None)
    embedding: list[float] = []
    key_embedding: list[float] = []
    key_text = ""
    if embedder is not None:
        # Same embed text as memory.set (topic-keying aside). Recompute the
        # key embedding too (its vector lives only on the embeddings table)
        # and mirror the prior ranking signals forward, else FINAL surfaces
        # zeroed columns. Humanized key form must match memory.set's input or
        # the same key embeds two different ways.
        key_text = key_axis_text(existing.memory_key or "")
        embedding, key_embedding = await _embed_value_and_key(
            embedder,
            _build_embed_text(serialized_value, memory_key=existing.memory_key),
            key_text,
        )
    conflict_result = None
    if embedder is not None:
        conflict_result = await _update_conflict_check(
            input,
            existing,
            serialized_value=serialized_value,
            embedding=embedding,
            cfg_task=cfg_task,
            op=op,
        )
    duplicate_of_id, status_cols = _status_columns(conflict_result)
    revision_run_id = _revision_run_id(input, existing)
    # value_json only for dict-shaped values (CH JSON rejects primitives).
    # memory_id is preserved (carried from `existing`) — usage events and
    # peers' conflict_with arrays key on it, so rotating it would dangle them.
    memory_row = memory_repo.build_memory_version_row(
        existing,
        value=serialized_value,
        value_json=(serialized_value if isinstance(new_value, dict) else "{}"),
        version=new_version,
        updated_at=memory_repo.written_now(),
        run_id=revision_run_id,
        # This version's author is the reviser, not the original writer.
        actor=app_provenance.writer_actor(fallback=existing.actor),
        tombstoned_at=None,
        char_limit=effective_char_limit,
        content_sha256=new_sha,
        tags=new_tags,
        concepts=new_concepts,
        sensitivity=sensitivity,
        **({"expires_at": None} if input.revive else {}),
        **({"confidence": float(new_confidence)} if new_confidence is not None else {}),
        **status_cols,
    )
    mark = await same_key_mark(
        conflict_result,
        org=input.org,
        namespace=existing.namespace,
        memory_row=memory_row,
        policy=(await cfg_task).policy,
    )
    # If searchable, refresh the embedding row too — in the same round trip
    # as the version row (as memory.set's dual write).
    emb_rows = (
        [
            _update_embedding_row(
                existing,
                memory_row,
                embedder,
                embedding,
                key_embedding,
                has_key=bool(key_text),
                conflict_checked_at=mark,
            )
        ]
        if embedder is not None
        else []
    )
    try:
        # Every insert runs to its end, so the fence below comes after the retire.
        memory_done, *others_done = await asyncio.gather(
            insert_memory_versions([memory_row], ch=ch),
            *([memory_repo.insert_embedding_rows(emb_rows, ch=ch)] if emb_rows else []),
            *(
                []
                if searchable
                else [memory_repo.tombstone_vectors_of(existing, version=new_version, ch=ch)]
            ),
            return_exceptions=True,
        )
        if not isinstance(memory_done, BaseException):
            # The row is in: announce it even when the rest of the write fails; a retry
            # writes and announces a new version.
            await _record_revision(
                memory_row,
                op=op,
                verdict=conflict_result.verdict if conflict_result is not None else "",
            )
        for done in [memory_done, *others_done]:
            if isinstance(done, BaseException):
                raise done
    finally:
        if sensitivity == "high":
            # After the retire (at the same version, the later insert wins the tie),
            # and even when an insert failed or the write was cancelled: the version
            # row may be in.
            await fence_high([(memory_repo.embedding_key(existing), new_version)], ch=ch)
    # Its version was drawn when the write began: a slow write indexes its own BM25 stats.
    after_write.index_late_rows(emb_rows, ch=ch)
    cfg = await cfg_task

    follows: list[Follow] = []
    conflict_result = await _update_post_write(input, existing, conflict_result, ch, follows)

    # Same-key supersession, exactly as memory.set runs it after its insert.
    await _revision_supersession(input.org, existing, serialized_value, ch, cfg.policy, follows)
    await _revised_edges(existing, memory_row, new_value, new_sha, sensitivity, op=op, ch=ch)
    await project_stated_relations(
        memory_row, new_value, input.relations, revised=True, op=op, ch=ch
    )
    # After the revision's own relations, so the follow sees the successor they state.
    follow_beside_response(
        org=input.org,
        namespace=existing.namespace,
        pairs=follows,
        op=op,
        agent=str(existing.agent),
        user_id=str(existing.user_id),
    )
    return MemoryUpdateByIdOutput(
        updated=True,
        new_version=new_version,
        content_sha256=new_sha,
        conflict_verdict=conflict_result.verdict if conflict_result is not None else "",
        conflict_peers=len(conflict_result.peers) if conflict_result is not None else 0,
        duplicate_of=duplicate_of_id,
    )
