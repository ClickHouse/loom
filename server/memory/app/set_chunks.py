"""Verbatim chunks for a long `memory.set` value.

`memory.set` embeds the first `EXCERPT_LIMIT` characters of a value, so the rest of a long
value (an investigation note, an evidence dump, a report stored as one memory) would be
findable only through that head. A searchable value whose text runs past the excerpt is
therefore also written to `loom.memory_chunks` in the 800/100 geometry
`memory.set_from_messages` uses, with `parent_memory_key` set to the memory key so the
search's chunk plane lifts the parent fact.

Chunk rows are keyed `(org, namespace, session_id, chunk_index,
content_sha256)`; a key's chunks share the deterministic session id
`memory.set:<memory_key>` so a re-set of the key replaces its own chunks:
unchanged chunks collapse under ReplacingMergeTree, chunks that no longer
exist are tombstoned (a replacing row with `tombstoned_at` set), new ones are
inserted. `memory.delete` tombstones the key's whole set.

Imports only the chunker and leaf services; `server.memory.app.handlers` imports
this module lazily, at the call site.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from server.core.ch_writes import committed_insert_settings
from server.core.datetimes import naive_utc_now
from server.memory.app.chunk_projection import chunk_text_with_ranges, chunk_text_with_truncation
from server.memory.app.sensitive_passages import embed_chunks_of

log = logging.getLogger(__name__)

# The session id (and `source_url`) a key's chunks are written under. Both
# derive from the key alone so a later write for the same key finds them by
# primary key and a reader can tell a memory.set chunk from a conversation one.
SESSION_PREFIX = "memory.set:"

# Fallback for `settings.memory_set_chunk_max_chunks` when the setting is
# unreadable: 48 chunks at 800/100 is ~34 KB of text.
_MAX_CHUNKS_FALLBACK = 48

# Every persisted column of loom.memory_chunks a tombstone must carry forward
# so the replacing row, once merged, is the only copy and loses nothing.
_CHUNK_COLUMNS = (
    "agent",
    "user_id",
    "session_id",
    "run_id",
    "chunk_index",
    "content_sha256",
    "content",
    "embedding",
    "embedding_model",
    "embedding_dim",
    "parent_memory_key",
    "line_start",
    "line_end",
    "modality",
    "caption",
    "source_url",
    "source_local_path",
)


def set_chunk_session_id(memory_key: str) -> str:
    """The deterministic `session_id` of a key's memory.set chunks."""
    return f"{SESSION_PREFIX}{memory_key}"


def human_text(value: Any, memory_type: str = "") -> str:
    """The text of a value as a reader would see it: a string as itself, a
    dict/list as indented JSON (one key or item per line, so the chunker's
    line and paragraph boundaries fall between fields, not inside them),
    anything else as compact JSON. A procedure chunks its text only
    (situation, name, steps), never its graph and counters."""
    if value is None:
        return ""
    if memory_type == "procedural":
        from server.memory.domain.embed_text import procedural_text_source

        text = procedural_text_source(memory_type, value)
        if text:
            return text
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, indent=1)
    return json.dumps(value, ensure_ascii=False)


def _max_chunks() -> int:
    from server.settings import settings

    value = settings.memory_set_chunk_max_chunks
    return value if value > 0 else _MAX_CHUNKS_FALLBACK


def _sha_text(value: Any) -> str:
    """`content_sha256` is a FixedString(64): the driver hands it back as
    bytes, and a bytes/str mismatch would keep every old chunk alive."""
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    return str(value or "")


async def _key_chunks(
    ch: Any,
    *,
    org: str,
    namespace: str,
    memory_key: str,
    user_id: str,
    strong: bool = False,
    tombstoned: bool = False,
) -> list[dict[str, Any]]:
    """The key's live memory.set chunks that belong to `user_id`: a primary-key
    read under FINAL so a chunk already tombstoned by an earlier write is not
    tombstoned twice. Every user holding the key shares its chunk session, so
    the read keeps to the writer's own chunks and those stamped '-' (no user;
    they may be the writer's) — never another user's. `tombstoned` reads the
    retired ones instead."""
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT {", ".join(_CHUNK_COLUMNS)}, created_at
        FROM loom.memory_chunks FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND session_id = {{sid:String}}
          AND parent_memory_key = {{key:String}}
          AND user_id IN ('-', {{uid:String}})
          AND tombstoned_at IS {"NOT NULL" if tombstoned else "NULL"}
        ORDER BY chunk_index ASC
        {_sequential() if strong else ""}
        """,
        params={
            "org": org,
            "ns": namespace,
            "sid": set_chunk_session_id(memory_key),
            "key": memory_key,
            "uid": user_id or "-",
        },
    )
    return rows


async def tombstone_set_chunks(
    *,
    org: str,
    namespace: str,
    memory_key: str,
    user_id: str,
    ch: Any = None,
    keep: set[tuple[int, str]] | frozenset[tuple[int, str]] = frozenset(),
    only: set[tuple[int, str]] | frozenset[tuple[int, str]] | None = None,
    now: datetime | None = None,
    strong: bool = False,
) -> int:
    """Tombstone `user_id`'s live memory.set chunks of the key (and the '-'
    ones) except those in `keep` (`(chunk_index, content_sha256)` pairs the
    caller is about to re-insert live), or only those in `only` when given. `strong`
    reads them with sequential consistency. Each tombstone is a replacing row
    (`_chunk_state_row`). Returns the number of tombstones written."""
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    rows = await _key_chunks(
        ch, org=org, namespace=namespace, memory_key=memory_key, user_id=user_id, strong=strong
    )
    stamp = (now or datetime.now(UTC)).replace(tzinfo=None)
    tombstones: list[dict[str, Any]] = []
    for r in rows:
        ident = (int(r.get("chunk_index") or 0), _sha_text(r.get("content_sha256")))
        if ident in keep or (only is not None and ident not in only):
            continue
        tombstones.append(_chunk_state_row(r, org=org, namespace=namespace, stamp=stamp))
    if tombstones:
        await _insert_chunk_states(ch, tombstones)
    return len(tombstones)


def _chunk_state_row(
    r: dict[str, Any],
    *,
    org: str,
    namespace: str,
    stamp: datetime | None = None,
    live: bool = False,
) -> dict[str, Any]:
    """The replacing row that retires chunk row `r` at `stamp`, or with `live`
    puts it back: every persisted column carried forward and `tombstoned_at`
    set to `stamp`, or None. The version column `created_at` is also the
    monthly partition key, so it keeps `r`'s value: a tombstone in a later
    month's partition would never merge with the row it retires, and every
    read without FINAL (and every merged part) would keep the retired chunk.
    Inserted after `r` with an equal version, the replacing row wins the merge
    and FINAL (the later insert of a tie). `stamp` stands in for `created_at`
    only when `r` carries none."""
    row = {c: r.get(c) for c in _CHUNK_COLUMNS}
    row.update(
        {
            "org": org,
            "namespace": namespace,
            "content_sha256": _sha_text(r.get("content_sha256")),
            "chunk_index": int(r.get("chunk_index") or 0),
            "embedding": list(r.get("embedding") or []),
            "embedding_dim": int(r.get("embedding_dim") or 0),
            "line_start": int(r.get("line_start") or 0),
            "line_end": int(r.get("line_end") or 0),
            "run_id": str(r.get("run_id") or "-"),
            "user_id": str(r.get("user_id") or "-"),
            "created_at": r.get("created_at") or stamp,
            "tombstoned_at": None if live else stamp,
        }
    )
    return row


async def _insert_chunk_states(ch: Any, rows: list[dict[str, Any]]) -> None:
    # Synchronous: the caller's next search must not lift the parent
    # through a chunk of the value it just replaced or deleted.
    await ch.insert(
        "loom.memory_chunks",
        rows,
        settings=committed_insert_settings(),
    )


async def tombstone_set_chunks_fail_open(
    *,
    org: str,
    namespace: str,
    memory_key: str,
    user_id: str,
    ch: Any = None,
    only: frozenset[tuple[int, str]] | None = None,
) -> int:
    """`tombstone_set_chunks` for a delete, a move or a consolidation: the fact's own new
    version is the record of it; a chunk failure is logged, never raised."""
    try:
        # Read with sequential consistency: a chunk another call just put back is retired.
        return await tombstone_set_chunks(
            org=org,
            namespace=namespace,
            memory_key=memory_key,
            user_id=user_id,
            ch=ch,
            only=only,
            strong=True,
        )
    except Exception as e:  # noqa: BLE001 — the fact's own new version stands
        log.warning("chunk tombstones failed for %s (non-fatal): %s", memory_key, e)
        return 0


async def tombstone_row_chunks_fail_open(
    row: Any, *, ch: Any = None, only_its_own: bool = False
) -> int:
    """`tombstone_set_chunks_fail_open` for the memory row a delete or move
    retires: its key, under its own user. `only_its_own` retires only the chunks
    this row's own value writes, so a newer value's chunks of the key stay."""
    only = set_chunk_identities(row.value, row.memory_type) if only_its_own else None
    return await tombstone_set_chunks_fail_open(
        org=row.org,
        namespace=row.namespace,
        memory_key=row.memory_key,
        user_id=str(row.user_id or "-"),
        ch=ch,
        only=only,
    )


def set_chunk_identities(value: Any, memory_type: str) -> frozenset[tuple[int, str]]:
    """The `(chunk_index, content_sha256)` of each chunk `write_set_chunks` writes for
    `value`: the identities of that value's chunk rows."""
    chunks, _truncated = plan_set_chunks(value, memory_type)
    return frozenset((i, _chunk_sha(chunk)) for i, (chunk, _start, _end) in enumerate(chunks))


async def restore_set_chunks(
    *,
    org: str,
    namespace: str,
    memory_key: str,
    user_id: str,
    identities: frozenset[tuple[int, str]],
    ch: Any,
) -> int:
    """Put back, live, the key's tombstoned memory.set chunks among `identities` (under
    `user_id` and '-'), read with sequential consistency. Each copy keeps the tombstone's
    `created_at` and is inserted after it, so it wins the tie, while a chunk a newer write
    re-inserts later still wins on its own `created_at`. Returns the number put back."""
    if not identities:
        return 0
    rows = await _key_chunks(
        ch,
        org=org,
        namespace=namespace,
        memory_key=memory_key,
        user_id=user_id,
        strong=True,
        tombstoned=True,
    )
    live = [
        _chunk_state_row(r, org=org, namespace=namespace, live=True)
        for r in rows
        if (int(r.get("chunk_index") or 0), _sha_text(r.get("content_sha256"))) in identities
    ]
    if live:
        await _insert_chunk_states(ch, live)
    return len(live)


async def _embed_chunks(embedder: Any, chunks: list[str]) -> list[list[float]]:
    """One batched request when the embedder can batch, serial otherwise (the
    local embedder has no `embed_many`); a chunk whose embedding fails is
    dropped from the write, not the whole value."""
    embed_many = getattr(embedder, "embed_many", None)
    if embed_many is not None:
        try:
            vectors = await embed_many(chunks)
            if len(vectors) != len(chunks):
                raise RuntimeError(
                    f"embed_many returned {len(vectors)} vectors for {len(chunks)} chunks"
                )
            return [[float(x) for x in v] for v in vectors]
        except Exception as e:  # noqa: BLE001 — falls back to serial embeds
            log.warning("memory.set chunks: batch embed failed; falling back serial: %s", e)
    serial: list[list[float]] = []
    for i, chunk in enumerate(chunks):
        try:
            serial.append([float(x) for x in await embedder.embed(chunk)])
        except Exception as e:  # noqa: BLE001 — that chunk stays unembedded
            log.warning("memory.set chunks: embed failed for chunk %d: %s", i, e)
            serial.append([])
    return serial


def plan_set_chunks(
    value: Any, memory_type: str, *, limit: int | None = None
) -> tuple[list[tuple[str, int, int]], int]:
    """The `(chunk, line_start, line_end)` passages `write_set_chunks` would
    write for a value, and the trailing characters the chunk cap leaves out;
    `([], 0)` when the value is short or the type is not searchable. Pure: no
    embedding, no read, no write."""
    return chunk_text_with_truncation(
        _chunked_text(value, memory_type, limit), max_chunks=_max_chunks()
    )


def _chunked_text(value: Any, memory_type: str, limit: int | None) -> str:
    """The human text of a value that is chunked, or "" when the value is short or the type
    is not searchable."""
    from server.memory.domain.memory_types import is_searchable_type

    if limit is None:
        # The handler's excerpt limit: what the row's own vector already covers.
        from server.memory.domain.excerpt import EXCERPT_LIMIT

        limit = EXCERPT_LIMIT
    if not is_searchable_type(memory_type):
        return ""
    text = human_text(value, memory_type)
    return text if len(text) > limit else ""


def _chunk_sha(chunk: str) -> str:
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()


async def write_set_chunks(
    input: Any,
    *,
    memory_type: str | None = None,
    limit: int | None = None,
    ch: Any = None,
    embedder: Any = None,
) -> int:
    """Write the verbatim chunks of a long memory.set value; the number of
    chunk rows written (0 when the value is short, the type is not
    searchable, or the setting is off).

    Called AFTER the fact row and its embedding are in: the chunks are a
    second way to find the row, never a condition of writing it. The text
    chunked is the human text of the value (`human_text`), not the JSON the
    row stores, so a chunk reads like the passage it came from.
    """
    if memory_type is None:
        memory_type = str(getattr(input, "memory_type", "") or "")
    cap = _max_chunks()
    text = _chunked_text(input.value, memory_type, limit)
    chunks, ranges, truncated_chars = chunk_text_with_ranges(text, max_chunks=cap)
    if truncated_chars:
        from server.memory.app.drops import record_drop

        log.warning(
            "memory.set chunks: value exceeded the %d-chunk cap; %d trailing chars "
            "not chunked for %s/%s key=%s",
            cap,
            truncated_chars,
            input.org,
            input.namespace,
            input.memory_key,
        )
        record_drop(
            input.org,
            input.namespace,
            "chunk.split",
            "truncated",
            item=f"{SESSION_PREFIX}{input.memory_key} chars={truncated_chars}",
            n=1,
        )
    if not chunks:
        return 0
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    if embedder is None:
        from server.embedding import get_embedder

        embedder = get_embedder()

    # A chunk that reads as high is held whatever its row's label: the chunk read path and the
    # provider rebuild decide by the text too.
    embedded = await embed_chunks_of(
        text, ranges, [c for c, _, _ in chunks], lambda texts: _embed_chunks(embedder, texts)
    )
    session_id = set_chunk_session_id(input.memory_key)
    run_id = str(getattr(input, "run_id", "") or "-")
    now = naive_utc_now()
    rows: list[dict[str, Any]] = []
    for i, ((chunk, line_start, line_end), e) in enumerate(zip(chunks, embedded, strict=True)):
        if not e.written:
            continue
        rows.append(
            {
                "org": input.org,
                "namespace": input.namespace,
                "agent": getattr(input, "agent", "") or "",
                "user_id": str(getattr(input, "user_id", "") or "-"),
                "session_id": session_id,
                "run_id": run_id,
                "chunk_index": i,
                "content_sha256": _chunk_sha(chunk),
                "content": chunk,
                "embedding": e.vector,
                "embedding_model": embedder.model_name,
                "embedding_dim": len(e.vector),
                "parent_memory_key": input.memory_key,
                "line_start": int(line_start),
                "line_end": int(line_end),
                "source_url": session_id,
                "created_at": now,
                "tombstoned_at": None,
            }
        )
    # The previous value's chunks that this value does not re-assert go
    # first, so the window in which both values are findable is the insert's.
    keep = {(int(r["chunk_index"]), str(r["content_sha256"])) for r in rows}
    await tombstone_set_chunks(
        org=input.org,
        namespace=input.namespace,
        memory_key=input.memory_key,
        user_id=str(getattr(input, "user_id", "") or "-"),
        ch=ch,
        keep=keep,
        now=now,
    )
    if not rows:
        return 0
    # Synchronous for the reason the fact insert is: the chunk plane is read
    # on the default search path, and a query landing before the chunks embed
    # must not find the head of the value and miss its body.
    await ch.insert(
        "loom.memory_chunks",
        rows,
        settings=committed_insert_settings(),
    )
    # A newer write may have made the key 'high' (or unsearchable) or deleted it while
    # these chunks were embedding. It inserts its row before tombstoning the key's
    # chunks, so either its tombstone saw these or this strong read sees its row. A
    # failed read is treated as "not served": the chunks are retired either way.
    served = False
    try:
        served = await _head_still_embedded(input, ch=ch)
    finally:
        if not served:
            await tombstone_set_chunks(  # only what this call wrote: a newer value's stay
                org=input.org,
                namespace=input.namespace,
                memory_key=input.memory_key,
                user_id=str(getattr(input, "user_id", "") or "-"),
                ch=ch,
                only=keep,
                now=now,
                strong=True,
            )
    return len(rows) if served else 0


def _sequential() -> str:
    from server.memory.store import _SEQUENTIAL, read_your_writes_enabled

    return _SEQUENTIAL if read_your_writes_enabled() else ""


async def _head_still_embedded(input: Any, *, ch: Any) -> bool:
    """The key's live head, read strongly, is a row search may serve."""
    import server.memory.store as memory_repo
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.domain.memory_types import is_embedded

    head = await memory_repo.get_by_key(
        memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent=getattr(input, "agent", "") or "",
            user_id=str(getattr(input, "user_id", "") or "-"),
            session_id=str(getattr(input, "session_id", "") or "-"),
        ),
        input.memory_key,
        fresh=True,
        strong=True,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    return head is not None and is_embedded(head.memory_type, head.sensitivity)


async def write_set_chunks_fail_open(
    input: Any,
    *,
    memory_type: str | None = None,
    limit: int | None = None,
    ch: Any = None,
    embedder: Any = None,
) -> int:
    """`write_set_chunks` for the memory.set write path: the fact row is
    already in, so a chunk failure is logged and the write answers with
    `chunks_written=0`, never an error."""
    try:
        return await write_set_chunks(
            input, memory_type=memory_type, limit=limit, ch=ch, embedder=embedder
        )
    except Exception as e:  # noqa: BLE001 — the fact row is written
        log.warning(
            "memory.set: verbatim chunks not written for %s (non-fatal): %s",
            getattr(input, "memory_key", ""),
            e,
        )
        return 0
