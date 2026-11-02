"""memory.move — rename a key, leaving the old key readable.

Deleting keys a running agent still reads breaks it: its exact-key `memory.get` answers
`found: false` and the agent falls back to its own storage. A move is a rename that keeps
the old key answering. It is a duplicate the caller declared: the destination is written
through the real `memory.set` path and the SOURCE row gains a version with
`status='duplicate'` and `duplicate_of` = the destination's memory_id — exactly the shape a
hash-collapsed duplicate has (search and list hide it, `memory.get(follow: true)` serves the
canonical), so every reader that already understands duplicates understands a move.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

import server.memory.store as memory_repo
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import served_content_sha256
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import MemoryDeleteInput, MemorySetInput
from server.memory.api.models_move import MemoryMoveInput, MemoryMoveOutput
from server.memory.app import handlers as _mem
from server.memory.app import set_chunks as _set_chunks
from server.memory.app import webhooks
from server.memory.app import write as write_mod
from server.memory.domain import lock as _lock
from server.memory.domain import provenance as _prov
from server.memory.domain.delete_marks import carried_trace
from server.memory.domain.memory_types import is_searchable_type
from server.memory.store.rows import next_version

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

log = logging.getLogger(__name__)

_PREVIEW_CHARS = 200


def _refuse(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _value_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


async def _load_source(input: MemoryMoveInput) -> MemoryRow:
    """The live source row, or the refusal the contract names: 400 on a
    self-move, 404 when the key is missing or tombstoned, 409
    ALREADY_REDIRECTED when it is already a duplicate (a second move of the
    same key), 409 PRECONDITION_FAILED on a hash mismatch."""
    if input.from_key == input.to_key:
        raise _refuse(400, "SAME_KEY", "from_key and to_key are the same key")
    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    # fresh: a move right after the set that wrote the key must see it.
    source = await memory_repo.get_by_key(
        identity, input.from_key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
    )
    if source is None or source.tombstoned_at is not None:
        raise _refuse(404, "NOT_FOUND", f"no live row under {input.from_key!r}")
    if str(source.status or "") == "duplicate":
        raise _refuse(
            409,
            "ALREADY_REDIRECTED",
            f"{input.from_key!r} already redirects to another row; move that row instead",
            duplicate_of=str(source.duplicate_of or ""),
        )
    if input.if_match_sha256:
        current = served_content_sha256(source.value)
        if current != input.if_match_sha256:
            raise _refuse(
                409,
                "PRECONDITION_FAILED",
                f"if_match_sha256 does not match current row (current_sha256={current!r})",
                current_sha256=current,
            )
    return source


def _destination_input(input: MemoryMoveInput, source: MemoryRow) -> Any:
    """The memory.set that writes the destination: the source's columns
    unless an override says otherwise, under the source row's own identity
    (a rename stays with the row's writer). `source_class` is carried only
    when the source's class is one a caller may declare (external / imported
    / derived); the server stamps every other class from the token and the
    op, so an `asserted` source lands `asserted` again."""
    source_class = str(getattr(source, "source_class", "") or "")
    return MemorySetInput(
        org=source.org,
        namespace=source.namespace,
        agent=source.agent,
        user_id=source.user_id,
        session_id=source.session_id,
        run_id=input.run_id,
        memory_key=input.to_key,
        memory_type=input.memory_type or source.memory_type,
        value=source.value if input.value is None else input.value,
        tags=list(source.tags or []) if input.tags is None else list(input.tags),
        entities=list(source.entities or []) if input.entities is None else list(input.entities),
        confidence=float(source.confidence),
        # A row an older build restored from a delete_version tombstone still holds its
        # marker, which no write may carry; the move's row drops it, as the row builder does.
        source_trace=carried_trace(source.source_trace),
        source_class=source_class if source_class in _prov.CALLER_DECLARABLE else None,
        source_resource_id=input.source_resource_id or (source.source_resource_id or ""),
        valid_at=input.valid_at if input.valid_at is not None else source.valid_at,
        valid_to=source.valid_to,
        pinned=bool(source.pinned),
        char_limit=source.char_limit,
        sensitivity=str(getattr(source, "sensitivity", "") or ""),  # type: ignore[arg-type]
        classify=False,
    )


async def _redirect_source(source: MemoryRow, dest_id: str, ch: Any) -> None:
    """The redirect: a new version of the SOURCE row with `status='duplicate'` and
    `duplicate_of` = the destination, every other column carried forward (the builder + the
    typed insert). The search mirror is re-emitted with the same status so the dense leg's own
    predicate hides the row before the sort (the `dedup_collapse` pattern); the source's
    verbatim chunks are tombstoned. Both fail-open: the loom.memory version is the record of
    the move."""
    now = naive_utc_now()
    row = memory_repo.build_memory_version_row(
        source,
        version=next_version(),
        status="duplicate",
        duplicate_of=dest_id,
        updated_at=now,
    )
    await memory_repo.insert_memory_versions([row], ch=ch)
    # The source key now reads as a redirect to the destination.
    await webhooks.announce([("memory.revised", row)])
    try:
        mirror = await memory_repo.embedding_mirror_for_keys(
            source.org, source.namespace, [source.memory_key], ch=ch
        )
        mine = [
            r
            for r in mirror
            if (str(r.get("agent")), str(r.get("user_id")), str(r.get("session_id")))
            == (source.agent, source.user_id, source.session_id)
        ]
        await memory_repo.restamp_embedding_rows(
            mine, status="duplicate", version=row["version"], ch=ch
        )
    except Exception as e:  # noqa: BLE001 — the move stands
        log.warning(
            "memory.move: mirror restamp failed for %s (non-fatal): %s", source.memory_key, e
        )
    if is_searchable_type(source.memory_type):
        await _set_chunks.tombstone_row_chunks_fail_open(source, ch=ch)


async def _tombstone_source(source: MemoryRow) -> None:
    """`keep_redirect: false` — the delete path, which carries the mirror and
    the chunk tombstones itself."""
    await _mem.handle_memory_delete(
        MemoryDeleteInput(
            org=source.org,
            namespace=source.namespace,
            agent=source.agent,
            user_id=source.user_id,
            session_id=source.session_id,
            memory_key=source.memory_key,
        )
    )


async def _destination_id(set_out: Any, set_input: Any) -> str:
    """The destination row's memory_id: memory.set's, or — on a `merge` that
    reinforced an existing row and wrote nothing — the live row under the key."""
    dest_id = str(getattr(set_out, "memory_id", "") or "")
    if dest_id:
        return dest_id
    identity = memory_repo.Identity(
        org=set_input.org,
        namespace=set_input.namespace,
        agent=set_input.agent,
        user_id=set_input.user_id,
        session_id=set_input.session_id,
    )
    dest = await memory_repo.get_by_key(
        identity, set_input.memory_key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
    )
    if dest is None:
        raise _refuse(
            409,
            "DESTINATION_NOT_WRITTEN",
            f"memory.set left no live row under {set_input.memory_key!r}; the source is unchanged",
        )
    return str(dest.memory_id)


async def handle_memory_move(input: MemoryMoveInput) -> MemoryMoveOutput:
    source = await _load_source(input)
    # A redirect changes the source row's lifecycle, not its value; a
    # `no_delete` lock permits it, `read_only` does not. The delete path
    # asserts its own action.
    if input.keep_redirect:
        _lock.assert_unlocked(source, "value", op="memory.move")
    else:
        _lock.assert_unlocked(source, "delete", op="memory.move")
    set_input = _destination_input(input, source)
    if input.dry_run:
        return MemoryMoveOutput(
            from_key=input.from_key,
            to_key=input.to_key,
            redirected=False,
            dry_run=True,
            memory_type=str(set_input.memory_type),
            # a high value is read by key only, through an audited read
            value_preview=""
            if source.sensitivity == "high"
            else _value_text(set_input.value)[:_PREVIEW_CHARS],
            sensitivity=source.sensitivity,
        )
    set_out = await write_mod.handle_memory_set(set_input)
    dest_id = await _destination_id(set_out, set_input)
    if input.keep_redirect:
        await _redirect_source(source, dest_id, get_client())
    else:
        await _tombstone_source(source)
    return MemoryMoveOutput(
        from_key=input.from_key,
        to_key=input.to_key,
        redirected=bool(input.keep_redirect),
        memory_id=dest_id,
        version=int(getattr(set_out, "version", 0) or 0),
        content_sha256=str(getattr(set_out, "content_sha256", "") or ""),
        advisories=list(getattr(set_out, "advisories", []) or []),
        memory_type=str(set_input.memory_type),
    )
