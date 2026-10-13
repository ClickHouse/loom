"""memory.recall handler — the raw turns, by text and by time."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from fastapi import HTTPException

from server.core.ch import get_client, sequential_reads
from server.core.cursors import decode_cursor, encode_cursor
from server.core.errors import AuthInsufficientError
from server.memory.api.models_temporal import MemoryRecallInput, MemoryRecallOutput, _RecallTurn
from server.memory.domain import recall as svc


def _session_read(thread: str, scoped: MemoryRecallInput) -> str | None:
    """The session the read is bound to: `thread`, else the request's own. Under
    own_session, `thread` may not name another session."""
    own = scoped.session_id if scoped.session_id and scoped.session_id != "-" else None
    sid = thread or own
    if scoped.read_scope == "own_session" and sid != own:
        raise AuthInsufficientError("read_scope=own_session reads only the request's session_id")
    return sid


async def handle_memory_recall(input: MemoryRecallInput) -> MemoryRecallOutput:
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import pin_read_filters
    from server.memory.store import read_your_writes_enabled

    tokens = svc.query_tokens(input.query)
    if not tokens and input.since is None and input.until is None and not input.thread:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "RECALL_REQUIRES_FILTER",
                "message": (
                    "Provide a query with at least one non-stop-word token, "
                    "a since/until window, or a thread (session) to scope the read"
                ),
            },
        )

    # Read scope: the token's ceiling narrows the request; a pinned token never reads another
    # user's turns.
    scoped, _ = pin_read_filters(input, current_auth())
    uid = scoped.user_id if scoped.user_id and scoped.user_id != "-" else None
    sid = _session_read(input.thread, scoped)
    order = svc.resolve_order(input.order, tokens)

    cursor: tuple[Any, str] | None = None
    if input.cursor and order == "recent":
        try:
            cursor = decode_cursor(input.cursor)
        except Exception:  # noqa: BLE001 — any bad cursor is a 400
            raise HTTPException(
                status_code=400,
                detail={"code": "BAD_CURSOR", "message": "cursor is not one this op issued"},
            ) from None

    ch = get_client()
    flt = svc.RecallFilter(
        tokens=tokens,
        match=input.match,
        since=input.since,
        until=input.until,
        role=input.role,
        agent=input.agent,
        user_id=uid,
        session_id=sid,
    )
    page_sql, page_params = svc.build_recall_sql(
        flt, order=order, cursor=cursor, offset=input.offset, limit=input.top_k
    )
    count_sql, count_params = svc.build_count_sql(flt)
    ident = {"org": input.org, "ns": input.namespace}
    page_params.update(ident)
    count_params.update(ident)

    scope = sequential_reads() if read_your_writes_enabled() else nullcontext()
    with scope:
        rows = await ch.query_all(page_sql, params=page_params)
        total_row = await ch.query_one(count_sql, params=count_params)
    page, more = svc.page_rows(list(rows or []), input.top_k)
    total = int((total_row or {}).get("n") or 0)

    turns: list[_RecallTurn] = []
    chars = 0
    for r in page:
        content = str(r.get("content") or "")
        truncated = len(content) > input.max_chars
        shown = content[: input.max_chars]
        chars += len(shown)
        turns.append(
            _RecallTurn(
                evidence_id=str(r.get("eid") or ""),
                session_id=str(r.get("session_id") or ""),
                role=str(r.get("role") or ""),
                actor=str(r.get("actor") or ""),
                observed_at=str(r.get("observed") or "")[:19],
                content=shown,
                truncated=truncated,
                matched_tokens=int(r.get("matched") or 0),
            )
        )

    next_cursor = ""
    next_offset = -1
    if more and page:
        if order == "recent":
            last = page[-1]
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(str(last.get("observed"))[:19])
                next_cursor = encode_cursor(ts, str(last.get("eid") or ""))
            except Exception:  # noqa: BLE001 — the last page has no cursor
                next_cursor = ""
        else:
            next_offset = int(input.offset) + len(page)

    return MemoryRecallOutput(
        turns=turns,
        total_matched=total,
        returned=len(turns),
        has_more=more,
        next_cursor=next_cursor,
        next_offset=next_offset,
        order=order,  # type: ignore[arg-type]
        query_tokens=tokens,
        tokens_returned=chars // 4,
    )
