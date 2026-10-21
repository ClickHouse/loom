"""The verbatim-chunk projection of a memory write: the conversation cut into
overlapping chunks (loom.memory_chunks) with the evidence ids each line came
from."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from server.core.ch_writes import batched_insert_settings
from server.core.datetimes import naive_utc_now
from server.llm import customer
from server.memory.app.credential_passages import embed_chunks_of
from server.memory.app.projection_errors import record_input_projection_failure
from server.memory.domain import chunk_shape

log = logging.getLogger(__name__)


_MIN_CHUNK_SIZE = 20  # drop chunks shorter than this (mostly noise)
# Fallback when the setting is unreadable; the cap itself is
# `settings.memory_chunks_max_per_call`. 64 chunks is ~50K chars, which a
# heavy agent transcript (tool results carried whole) can exceed.
_MAX_CHUNKS_PER_CALL = 64


# A caller that replays observations (the deferred-extraction
# worker) wants the facts, not verbatim chunks of tool payloads — those are
# 800-char JSON fragments that outrank the facts on recall. Task-scoped;
# a normal set_from_messages keeps its chunk layer.
_CHUNK_LAYER_SUPPRESSED: ContextVar[bool] = ContextVar("loom_chunk_layer_suppressed", default=False)


@contextmanager
def suppress_chunk_layer() -> Iterator[None]:
    """Inside: `write_memory_chunks` is a no-op for this task."""
    token = _CHUNK_LAYER_SUPPRESSED.set(True)
    try:
        yield
    finally:
        _CHUNK_LAYER_SUPPRESSED.reset(token)


def chunk_layer_suppressed() -> bool:
    return _CHUNK_LAYER_SUPPRESSED.get()


def _max_chunks_per_call() -> int:
    from server.settings import settings

    value = int(settings.memory_chunks_max_per_call)
    return value if value > 0 else _MAX_CHUNKS_PER_CALL


# An enumeration is one unit at chunking time too. The list
# item a question asks for ("what was the fourth app", "the Jamaican snapper
# dish with fruit") sat across a chunk boundary on four of the five
# single-session-assistant misses of one chat-history QA run: the name
# ended one chunk and the description opened the next, or items 4–5 fell
# into a chunk the search did not rank. A boundary between two list lines
# is moved to the end of the list when the list fits in `_ENUM_MAX_CHARS`.
_ENUM_LINE_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*\u2022])\s+\S")
_ENUM_MAX_CHARS = 1600
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?\s+(?=[A-Z\"'(\[])")


def _last_sentence_end(text: str, lo: int, hi: int) -> int:
    """Index just after the last sentence end in text[lo:hi], or -1."""
    last = -1
    for m in _SENTENCE_END_RE.finditer(text, lo, hi):
        last = m.end()
    return last


def _extend_over_enumeration(text: str, start: int, end: int) -> int:
    """`end` moved past the enumeration a chunk boundary would otherwise
    split: when the line ending at `end` and the line starting there are
    both list lines, walk forward to the first non-list line, provided the
    whole list (from its first item) is at most `_ENUM_MAX_CHARS`."""
    if end >= len(text):
        return end
    before = text.rfind("\n", start, end - 1)
    prev_line = text[before + 1 : end]
    nl = text.find("\n", end)
    next_line = text[end : nl if nl != -1 else len(text)]
    if not (_ENUM_LINE_RE.match(prev_line) and _ENUM_LINE_RE.match(next_line)):
        return end
    # Walk back to the list's first line, forward to its last.
    list_start = before + 1
    while list_start > start:
        prev_nl = text.rfind("\n", start, list_start - 1)
        line = text[prev_nl + 1 : list_start]
        if not _ENUM_LINE_RE.match(line):
            break
        list_start = prev_nl + 1
    pos = end
    while pos < len(text):
        nl = text.find("\n", pos)
        line = text[pos : nl if nl != -1 else len(text)]
        if not _ENUM_LINE_RE.match(line):
            break
        pos = len(text) if nl == -1 else nl + 1
    if pos - list_start > _ENUM_MAX_CHARS:
        return end
    return pos


def chunk_text(text: str) -> list[tuple[str, int, int]]:
    """Split `text` into ~`chunk_shape.CHUNK_SIZE`-char chunks at semantic boundaries.

    Returns (chunk_text, line_start, line_end) triples, 1-indexed line numbers
    in the original text (operator UIs jump to a hit's range). Prefers a
    paragraph break, then a line break, then a char boundary — boundary search
    only past 50% of the window, so tight passages aren't over-split.
    """
    return chunk_text_with_truncation(text)[0]


def chunk_text_with_truncation(
    text: str, *, max_chunks: int | None = None
) -> tuple[list[tuple[str, int, int]], int]:
    """`chunk_text` plus the number of characters past the per-call chunk
    cap (`max_chunks`, default the setting) that were NOT chunked (0 = nothing lost).
    Callers with an identity record that tail in the drop ledger, so the cap
    never truncates silently."""
    chunks, _ranges, truncated = chunk_text_with_ranges(text, max_chunks=max_chunks)
    return chunks, truncated


def chunk_text_with_ranges(
    text: str, *, max_chunks: int | None = None
) -> tuple[list[tuple[str, int, int]], list[tuple[int, int]], int]:
    """`chunk_text_with_truncation` plus each chunk's [start, end) character range in
    `text`, so a caller can tell which chunks a span of the text falls in."""
    if not text:
        return [], [], 0
    cap = int(max_chunks) if max_chunks else _max_chunks_per_call()
    offset = len(text) - len(text.lstrip())
    text = text.strip()
    if len(text) < _MIN_CHUNK_SIZE:
        if not text:
            return [], [], 0
        return [(text, 1, text.count("\n") + 1)], [(offset, offset + len(text))], 0
    chunks: list[tuple[str, int, int]] = []
    ranges: list[tuple[int, int]] = []
    n = len(text)
    start = 0
    last_end = 0
    while start < n and len(chunks) < cap:
        end = min(start + chunk_shape.CHUNK_SIZE, n)
        if end < n:
            half = start + chunk_shape.CHUNK_SIZE // 2
            para_break = text.rfind("\n\n", half, end)
            if para_break != -1:
                end = para_break + 2
            else:
                line_break = text.rfind("\n", half, end)
                if line_break != -1:
                    end = line_break + 1
                else:
                    # A sentence end beats a raw character cut —
                    # "the GR-90. This trail takes you…" split at the character
                    # left the trail's name in the chunk before its description.
                    sent_break = _last_sentence_end(text, half, end)
                    if sent_break != -1:
                        end = sent_break
            end = _extend_over_enumeration(text, start, end)
        chunk = text[start:end].strip()
        if len(chunk) >= _MIN_CHUNK_SIZE:
            line_start = text.count("\n", 0, start) + 1
            line_end = line_start + chunk.count("\n")
            chunks.append((chunk, line_start, line_end))
            ranges.append((offset + start, offset + end))
        last_end = end
        if end >= n:
            break
        start = max(start + 1, end - chunk_shape.CHUNK_OVERLAP)
    # The loop only stops short of the end when the chunk cap is hit; the
    # tail past the last chunk's end is what nobody will ever be able to find.
    return chunks, ranges, max(0, n - last_end)


def message_index_to_evidence_id(
    msgs: list[dict[str, str]],
    evidence_ids: list[str],
) -> dict[int, str]:
    """Map current_messages indices to the evidence IDs written for them."""

    out: dict[int, str] = {}
    evidence_cursor = 0
    for idx, msg in enumerate(msgs):
        if not str(msg.get("content") or "").strip():
            continue
        if evidence_cursor >= len(evidence_ids):
            break
        out[idx] = evidence_ids[evidence_cursor]
        evidence_cursor += 1
    return out


def message_line_evidence_refs(
    msgs: list[dict[str, str]],
    evidence_ids: list[str],
) -> list[tuple[int, int, str]]:
    """Map source-message evidence IDs to line ranges in chunk text."""

    evidence_by_index = message_index_to_evidence_id(msgs, evidence_ids)
    if not evidence_by_index:
        return []
    refs: list[tuple[int, int, str]] = []
    current_line = 1
    for idx, msg in enumerate(msgs):
        formatted = f"[{msg.get('role', 'user')}] {msg.get('content', '')}"
        line_count = max(1, formatted.count("\n") + 1)
        evidence_id = evidence_by_index.get(idx)
        if evidence_id:
            refs.append((current_line, current_line + line_count - 1, evidence_id))
        current_line += line_count
    return refs


def _conversation_text(msgs: list[dict[str, str]]) -> str:
    """The conversation as `[role] content` lines. A timestamped turn keeps its
    clock inside the verbatim text — chunks have no valid_at column, and a
    reader answering "around 08:10" from a chunk needs the minute the sentence
    was said. Untimestamped turns render exactly as before."""
    from server.memory.app.extract import _message_stamp

    def _line(m: Mapping[str, object]) -> str:
        stamp = _message_stamp(m)
        who = f"{m.get('role', 'user')} @ {stamp}" if stamp else str(m.get("role", "user"))
        return f"[{who}] {m.get('content', '')}"

    return "\n".join(_line(m) for m in msgs)


def _note_truncation(input: Any, truncated_chars: int) -> None:
    from server.memory.app.drops import record_drop

    log.warning(
        "memory_chunks: input exceeded the %d-chunk cap; %d trailing chars not chunked for %s/%s",
        _max_chunks_per_call(),
        truncated_chars,
        input.org,
        input.namespace,
    )
    record_drop(
        input.org,
        input.namespace,
        "chunk.split",
        "truncated",
        item=f"chars={truncated_chars}",
        n=1,
    )


async def _embed_chunks(embedder: Any, texts: list[str]) -> list[list[float]]:
    """One batch embed; on failure one embed per chunk, an unembeddable chunk
    answering []. A held call (`customer.HOLDS`) raises: no chunk is
    unembeddable then, and the caller keeps the projection to retry."""
    try:
        vectors = await embedder.embed_many(texts)
        if len(vectors) != len(texts):
            raise RuntimeError(
                "memory_chunks batch embed returned "
                f"{len(vectors)} vectors for {len(texts)} chunks",
            )
        return list(vectors)
    except customer.HOLDS:
        raise
    except Exception as e:  # noqa: BLE001 — falls back to one embed per chunk
        log.warning("memory_chunks batch embed failed; falling back serial: %s", e)
        out: list[list[float]] = []
        for i, chunk in enumerate(texts):
            try:
                out.append(await embedder.embed(chunk))
            except customer.HOLDS:
                raise
            except Exception as chunk_error:  # noqa: BLE001 — this chunk stays unembedded
                log.warning("memory_chunks embed failed for chunk %d: %s", i, chunk_error)
                out.append([])
        return out


async def write_memory_chunks(
    input: Any,
    msgs: list[dict[str, str]],
    parent_memory_keys: list[str] | None = None,
    evidence_ids: list[str] | None = None,
) -> None:
    """Write verbatim chunks of the conversation to loom.memory_chunks.

    Parallel to the extracted-fact layer — preserves raw text so future
    searches recover what the extractor missed. Same embedder + dim as the
    fact layer so cosine is comparable. Fail-open.
    """
    import hashlib
    import uuid

    from server.core.ch import get_client
    from server.embedding import get_embedder

    if not msgs or chunk_layer_suppressed():
        return

    # A chunk belongs to the run and the end user that wrote it, so a run-
    # or user-bounded search can keep to its own passages. '-' is the
    # identity sentinel.
    run_id = str(getattr(input, "run_id", "") or "-")
    user_id = str(getattr(input, "user_id", "") or "-")

    text = _conversation_text(msgs)
    chunks, ranges, truncated_chars = chunk_text_with_ranges(text)
    if truncated_chars:
        _note_truncation(input, truncated_chars)
    if not chunks:
        return

    embedder = get_embedder()
    ch = get_client()

    # Caller need not provide a session_id; generate a per-call UUID so chunks
    # from one ingest are co-grouped.
    session_id = getattr(input, "session_id", "") or uuid.uuid4().hex
    parent_keys = list(dict.fromkeys(k for k in (parent_memory_keys or []) if k))
    parent_memory_key = next((k for k in parent_keys if k), "")
    source_resource_id = (
        f"memory.set_from_messages:{session_id}" if session_id else "memory.set_from_messages"
    )

    now = naive_utc_now()
    evidence_line_refs = message_line_evidence_refs(msgs, evidence_ids or [])
    chunk_payloads = [
        (i, chunk, line_start, line_end) for i, (chunk, line_start, line_end) in enumerate(chunks)
    ]
    embedded = await embed_chunks_of(
        text,
        ranges,
        [chunk for _, chunk, _, _ in chunk_payloads],
        lambda texts: _embed_chunks(embedder, texts),
    )

    rows: list[dict[str, Any]] = []
    spans: list[tuple[int, str, int, int]] = []
    for (i, chunk, line_start, line_end), e in zip(chunk_payloads, embedded, strict=True):
        if not e.written:
            continue  # unembeddable: a plain chunk is written only with its vector
        content_sha256 = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        rows.append(
            {
                "org": input.org,
                "namespace": input.namespace,
                "agent": getattr(input, "agent", "") or "",
                "user_id": user_id,
                "session_id": session_id,
                "run_id": run_id,
                "chunk_index": i,
                "content_sha256": content_sha256,
                "content": chunk,
                "embedding": e.vector,
                "embedding_model": embedder.model_name,
                "embedding_dim": len(e.vector),
                "parent_memory_key": parent_memory_key,
                "line_start": int(line_start),
                "line_end": int(line_end),
                "source_url": source_resource_id,
                "created_at": now,
                "tombstoned_at": None,
            }
        )
        spans.append((int(i), content_sha256, int(line_start), int(line_end)))
    # One row per chunk x parent key x overlapping evidence turn, from the builder
    # the projection-failure retry shares.
    from server.memory.app.chunk_links import SOURCES_TABLE
    from server.memory.app.chunk_links import provenance_rows as _provenance_rows

    provenance_rows = _provenance_rows(
        org=input.org,
        namespace=input.namespace,
        agent=getattr(input, "agent", "") or "",
        session_id=session_id,
        chunks=spans,
        parent_keys=list(parent_keys or []),
        evidence_refs=[(int(a), int(b), str(e)) for a, b, e in evidence_line_refs],
        now=now,
    )

    if not rows:
        return

    # Synchronous for the same reason the caller is synchronous: chunks are read
    # on the default bm25_hybrid search path, so a query landing right after the
    # write must find them; a fire-and-forget insert would break that.
    await ch.insert(
        "loom.memory_chunks",
        rows,
        settings=batched_insert_settings(),
    )
    if provenance_rows:
        try:
            await ch.insert(
                SOURCES_TABLE,
                provenance_rows,
                settings={"async_insert": 1, "wait_for_async_insert": 0},
            )
        except Exception as e:  # noqa: BLE001 — recorded as a projection failure for retry
            log.warning("%s write failed (non-fatal): %s", SOURCES_TABLE, e)
            await record_input_projection_failure(
                input,
                projection="memory_chunk_sources",
                error=e,
                memory_keys=parent_keys,
                payload={
                    "session_id": session_id,
                    "chunk_count": len(rows),
                    "source_resource_id": source_resource_id,
                },
            )
