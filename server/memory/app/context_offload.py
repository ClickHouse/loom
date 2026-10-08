"""Handlers for context.offload / context.fetch (see memory/api/context_offload.py)."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now as _now
from server.core.errors import ValidationError
from server.memory.api.models_context_offload import (
    ContextFetchInput,
    ContextFetchOutput,
    ContextOffloadInput,
    ContextOffloadOutput,
)
from server.memory.store.rows import next_version
from server.settings import settings

_SUMMARY_CHARS = 200


def content_key(content: str) -> tuple[str, str]:
    """(sha256 hex, content-addressed blob key)."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest, f"blob/{digest[:20]}"


def default_summary(content: str, *, chars: int = _SUMMARY_CHARS) -> str:
    flat = " ".join(content.split())
    return flat if len(flat) <= chars else flat[: chars - 1].rstrip() + "…"


def render_pointer(blob_key: str, content_bytes: int, lines: int, summary: str) -> str:
    """The line that replaces the content in the transcript: what it is, how
    big, and exactly how to get it back."""
    return (
        f"[offloaded {content_bytes:,} bytes / {lines:,} lines → context.fetch "
        f"blob_key={blob_key} (offset/limit, or pattern=<regex>)] {summary}"
    ).rstrip()


def slice_content(content: str, offset: int, limit: int) -> tuple[str, int]:
    """(slice, next_offset) — next_offset is -1 at the end."""
    piece = content[offset : offset + limit]
    nxt = offset + len(piece)
    return piece, (nxt if nxt < len(content) else -1)


def grep_content(
    content: str, pattern: str, *, offset: int, limit: int, context_lines: int = 0
) -> tuple[str, int, int]:
    """Matching lines as `lineno: text` (1-based), with `context_lines` of
    surrounding context; `offset` skips that many matches. Returns
    (text, matched_lines_total, next_match_offset or -1)."""
    rx = re.compile(pattern, re.IGNORECASE)
    lines = content.splitlines()
    matches = [i for i, ln in enumerate(lines) if rx.search(ln)]
    out: list[str] = []
    used = 0
    emitted: set[int] = set()
    consumed = offset
    for i in matches[offset:]:
        lo, hi = max(0, i - context_lines), min(len(lines), i + context_lines + 1)
        chunk = [
            f"{j + 1}{':' if j == i else '-'} {lines[j]}" for j in range(lo, hi) if j not in emitted
        ]
        text = "\n".join(chunk)
        if out and used + len(text) + 1 > limit:
            break
        emitted.update(range(lo, hi))
        out.append(text)
        used += len(text) + 1
        consumed += 1
    nxt = consumed if consumed < len(matches) else -1
    return "\n".join(out), len(matches), nxt


def _identity_row(input: Any) -> dict[str, Any]:
    return {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
        "run_id": input.run_id,
    }


async def handle_context_offload(input: ContextOffloadInput) -> ContextOffloadOutput:
    from fastapi import HTTPException

    raw = input.content.encode("utf-8")
    max_bytes = int(settings.context_offload_max_bytes)
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"content is {len(raw):,} bytes; LOOM_CONTEXT_OFFLOAD_MAX_BYTES is {max_bytes:,}"
            ),
        )
    sha, auto_key = content_key(input.content)
    blob_key = input.blob_key.strip("/") or auto_key
    summary = input.summary.strip() or default_summary(input.content)
    now = _now()
    expires = now + timedelta(seconds=int(input.ttl_seconds))
    await get_client().insert(
        "loom.context_blobs",
        [
            {
                **_identity_row(input),
                "blob_key": blob_key,
                "content": input.content,
                "content_bytes": len(raw),
                "content_type": input.content_type,
                "summary": summary,
                "sha256": sha,
                "created_at": now,
                "expires_at": expires,
                "tombstoned_at": None,
                "version": next_version(),
            }
        ],
    )
    lines = input.content.count("\n") + (0 if input.content.endswith("\n") else 1)
    return ContextOffloadOutput(
        blob_key=blob_key,
        content_bytes=len(raw),
        content_chars=len(input.content),
        sha256=sha,
        summary=summary,
        expires_at=expires,
        pointer=render_pointer(blob_key, len(raw), lines, summary),
    )


async def handle_context_fetch(input: ContextFetchInput) -> ContextFetchOutput:
    if input.pattern:
        try:
            re.compile(input.pattern)
        except re.error as e:
            raise ValidationError(
                f"pattern is not a valid regular expression: {e}", code="BAD_PATTERN"
            ) from None
    # expires_at > now(): the TTL merge is retention, not visibility.
    row = await get_client().query_one(
        """
        SELECT content, content_type, summary, expires_at
        FROM loom.context_blobs FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND blob_key = {key:String}
          AND tombstoned_at IS NULL AND expires_at > now()
        LIMIT 1
        """,
        params={"org": input.org, "ns": input.namespace, "key": input.blob_key},
    )
    if row is None:
        return ContextFetchOutput(found=False, blob_key=input.blob_key)
    content = str(row.get("content") or "")
    base = ContextFetchOutput(
        found=True,
        blob_key=input.blob_key,
        content_type=str(row.get("content_type") or ""),
        summary=str(row.get("summary") or ""),
        total_chars=len(content),
        total_lines=len(content.splitlines()),
        offset=input.offset,
        expires_at=row.get("expires_at"),
    )
    if input.pattern:
        text, matched, nxt = grep_content(
            content,
            input.pattern,
            offset=input.offset,
            limit=input.limit,
            context_lines=input.context_lines,
        )
        return base.model_copy(
            update={"content": text, "matched_lines": matched, "next_offset": nxt}
        )
    piece, nxt = slice_content(content, input.offset, input.limit)
    return base.model_copy(update={"content": piece, "next_offset": nxt})
