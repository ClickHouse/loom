"""working_memory.* handlers.

Working memory is a small agent-editable scratchpad loaded into context,
not a retrieval corpus. It is scoped to (org, namespace, agent, user_id,
session_id) and kept in its own table so it cannot pollute memory.search.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from fastapi import HTTPException

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now as _now
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import row_in_read_scope
from server.memory.api.models_working_memory import (
    DEFAULT_CHAR_LIMIT,
    WorkingMemoryAppendInput,
    WorkingMemoryBlock,
    WorkingMemoryDeleteInput,
    WorkingMemoryDeleteOutput,
    WorkingMemoryGetInput,
    WorkingMemoryGetOutput,
    WorkingMemoryMutationOutput,
    WorkingMemoryRenderInput,
    WorkingMemoryRenderOutput,
    WorkingMemoryReplaceInput,
    WorkingMemorySetInput,
)
from server.memory.app.scratchpad import run_rows
from server.memory.store.rows import next_version

log = logging.getLogger(__name__)


def _trim_to_limit(content: str, char_limit: int) -> None:
    if len(content) > char_limit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "WORKING_MEMORY_LIMIT_EXCEEDED",
                "message": (
                    f"working-memory content length {len(content)} exceeds char_limit {char_limit}"
                ),
                "value_length": len(content),
                "char_limit": char_limit,
            },
        )


def fill_ratio(value_length: int, char_limit: int) -> float:
    """len/limit rounded to three places; 0.0 for a degenerate limit."""
    if char_limit <= 0:
        return 0.0
    return round(value_length / char_limit, 3)


def _scope_where(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (
        f"{p}org = {{org:String}} AND {p}namespace = {{ns:String}} "
        f"AND {p}agent = {{agent:String}} AND {p}user_id = {{uid:String}} "
        f"AND {p}session_id = {{sid:String}} AND {p}run_id = {{run:String}}"
    )


def _params(input: Any) -> dict[str, Any]:
    return {
        "org": input.org,
        "ns": input.namespace,
        "agent": input.agent,
        "uid": input.user_id,
        "sid": input.session_id,
        "run": input.run_id,
    }


def _row_identity(input: Any) -> dict[str, Any]:
    return {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
        "run_id": input.run_id,
    }


def _in_read_scope(input: Any) -> bool:
    """Blocks are read by their exact identity, so the request's own user and
    session decide whether the read scope admits them."""
    scope = request_scope()
    return row_in_read_scope(scope, input.user_id, input.session_id)


async def _get_row(input: Any, block_key: str) -> dict[str, Any] | None:
    """The block's head — the read `get` answers and `append` / `replace` /
    `delete` build their write from, so it must see the caller's own last
    write (a stale head made append drop the text before it)."""
    from server.core.ch import own_write_reads

    ch = get_client()
    params = {**_params(input), "key": block_key}
    with own_write_reads():
        return await ch.query_one(
            f"""
            SELECT block_key, content, char_limit, version, created_at, updated_at
            FROM loom.working_memory FINAL
            WHERE {_scope_where()}
              AND block_key = {{key:String}}
              AND tombstoned_at IS NULL
            LIMIT 1
            """,
            params=params,
        )


async def handle_working_memory_get(
    input: WorkingMemoryGetInput,
) -> WorkingMemoryGetOutput:
    if not _in_read_scope(input):
        return WorkingMemoryGetOutput(found=False, block_key=input.block_key)
    row = await _get_row(input, input.block_key)
    if row is None:
        return WorkingMemoryGetOutput(found=False, block_key=input.block_key)
    content = str(row.get("content") or "")
    char_limit = int(row.get("char_limit") or DEFAULT_CHAR_LIMIT)
    return WorkingMemoryGetOutput(
        found=True,
        block_key=str(row["block_key"]),
        content=content,
        char_limit=char_limit,
        version=int(row.get("version") or 0),
        updated_at=row.get("updated_at"),
        fill_ratio=fill_ratio(len(content), char_limit),
    )


async def _write_block(
    input: Any, *, content: str, char_limit: int, head: dict[str, Any] | None
) -> WorkingMemoryMutationOutput:
    """One version row of the block. `head` is the live row the mutation read
    (None for a blind `set`): its `created_at` is carried, so the block keeps
    the time it was first written."""
    _trim_to_limit(content, char_limit)
    now = _now()
    version = next_version()
    await get_client().insert(
        "loom.working_memory",
        [
            {
                **_row_identity(input),
                "block_key": input.block_key,
                "content": content,
                "char_limit": char_limit,
                "created_at": (head or {}).get("created_at") or now,
                "updated_at": now,
                "tombstoned_at": None,
                "version": version,
            }
        ],
    )
    return WorkingMemoryMutationOutput(
        block_key=input.block_key,
        version=version,
        char_limit=char_limit,
        value_length=len(content),
        fill_ratio=fill_ratio(len(content), char_limit),
    )


async def handle_working_memory_set(
    input: WorkingMemorySetInput,
) -> WorkingMemoryMutationOutput:
    """Write the block's content as given. A blind write: it reads no head,
    so over an existing block the new row's `created_at` is now."""
    return await _write_block(
        input,
        content=input.content,
        char_limit=int(input.char_limit or DEFAULT_CHAR_LIMIT),
        head=None,
    )


async def handle_working_memory_append(
    input: WorkingMemoryAppendInput,
) -> WorkingMemoryMutationOutput:
    """Read the head, write head + separator + addition. Read-modify-write
    with no version check: two concurrent appends to one block each build on
    the same head and the later version wins, dropping the other's text."""
    head = await _get_row(input, input.block_key)
    if head is None:
        return await _write_block(
            input, content=input.addition, char_limit=DEFAULT_CHAR_LIMIT, head=None
        )
    return await _write_block(
        input,
        content=str(head.get("content") or "") + input.separator + input.addition,
        char_limit=int(head.get("char_limit") or DEFAULT_CHAR_LIMIT),
        head=head,
    )


async def handle_working_memory_replace(
    input: WorkingMemoryReplaceInput,
) -> WorkingMemoryMutationOutput:
    """Replace `old` with `new` in the head (read-modify-write, as append)."""
    head = await _get_row(input, input.block_key)
    if head is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "WORKING_MEMORY_BLOCK_NOT_FOUND",
                "message": f"working-memory block {input.block_key!r} does not exist",
            },
        )
    content = str(head.get("content") or "")
    if input.old not in content:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SUBSTRING_NOT_FOUND",
                "message": f"substring {input.old!r} not found in {input.block_key!r}",
            },
        )
    return await _write_block(
        input,
        content=content.replace(input.old, input.new),
        char_limit=int(head.get("char_limit") or DEFAULT_CHAR_LIMIT),
        head=head,
    )


async def handle_working_memory_delete(
    input: WorkingMemoryDeleteInput,
) -> WorkingMemoryDeleteOutput:
    existing = await _get_row(input, input.block_key)
    if existing is None:
        return WorkingMemoryDeleteOutput(deleted=False, block_key=input.block_key)
    ch = get_client()
    now = _now()
    version = next_version()
    await ch.insert(
        "loom.working_memory",
        [
            {
                **_row_identity(input),
                "block_key": input.block_key,
                "content": str(existing.get("content") or ""),
                "char_limit": int(existing.get("char_limit") or DEFAULT_CHAR_LIMIT),
                "created_at": existing.get("created_at") or now,
                "updated_at": now,
                "tombstoned_at": now,
                "version": version,
            }
        ],
    )
    return WorkingMemoryDeleteOutput(deleted=True, block_key=input.block_key)


async def handle_working_memory_render(
    input: WorkingMemoryRenderInput,
) -> WorkingMemoryRenderOutput:
    if not _in_read_scope(input):
        return WorkingMemoryRenderOutput(rendered="", block_count=0, total_chars=0)
    ch = get_client()
    where = [_scope_where(), "tombstoned_at IS NULL"]
    params = _params(input)
    if input.block_keys:
        where.append("block_key IN {keys:Array(String)}")
        params["keys"] = list(input.block_keys)
    from server.core.ch import own_write_reads

    with own_write_reads():  # must see the note written a call ago
        rows = await ch.query_all(
            f"""
            SELECT block_key, content, char_limit, version, updated_at
            FROM loom.working_memory FINAL
            WHERE {" AND ".join(where)}
            ORDER BY block_key
            LIMIT {{lim:UInt32}}
            """,
            params={**params, "lim": int(input.limit)},
        )
    blocks = [
        WorkingMemoryBlock(
            block_key=str(row["block_key"]),
            content=str(row.get("content") or ""),
            char_limit=int(row.get("char_limit") or DEFAULT_CHAR_LIMIT),
            version=int(row.get("version") or 0),
            updated_at=row.get("updated_at"),
        )
        for row in rows
    ]
    if input.format == "markdown":
        rendered = "\n\n".join(f"## {b.block_key}\n{b.content}" for b in blocks)
    else:
        rendered = "\n".join(
            (
                f'<working-memory block="{escape(b.block_key, quote=True)}">'
                f"{escape(b.content)}"
                "</working-memory>"
            )
            for b in blocks
        )
    return WorkingMemoryRenderOutput(
        rendered=rendered,
        blocks=blocks,
        block_count=len(blocks),
        total_chars=sum(len(b.content) for b in blocks),
    )


async def render_run_blocks(
    org: str,
    namespace: str,
    run_id: str,
    *,
    limit: int = 5,
    max_chars: int = 4000,
    ch: Any | None = None,
) -> str:
    """The run's working-memory blocks as markdown, newest first, for a reader that has
    the run_id but not the writer's full identity: memory.ask is asked with agent="*",
    while the run scope wrote its notes as the agent it runs as. The run_id is unique to
    one execution, so (org, namespace, run_id) is the scope, narrowed by the request's
    read scope (`scratchpad.run_rows`). '' when the run has no blocks or the read fails;
    a refused read scope raises, as the rest of the call would."""
    if not run_id or run_id == "-":
        return ""
    request_scope()  # a refusal is the caller's error, not a failed read
    ch = ch or get_client()
    try:
        rows = await run_rows(ch, org, namespace, run_id, limit=limit)
    except Exception as e:  # noqa: BLE001 — a scratchpad read never blocks an answer
        log.warning("working_memory: run block read failed (open): %s", e)
        return ""
    parts = [
        f"## {row['block_key']}\n{str(row.get('content') or '').strip()}"
        for row in rows
        if str(row.get("content") or "").strip()
    ]
    text = "\n\n".join(parts)
    return text[:max_chars]
