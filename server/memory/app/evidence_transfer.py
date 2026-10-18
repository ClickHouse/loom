"""Handlers for memory.export_evidence / memory.import_evidence.

Export pages `loom.memory_chunks` (with the chunk provenance links per chunk, in either
link layout: per chunk, or compact per session) or `loom.session_summaries` for one
namespace without embeddings.
Import re-embeds with the server's embedder and inserts the rows under the
importing identity, so the chunk leg — which filters on `embedding_model` —
sees them on the next search. Same row shapes as the extraction pipeline's
own writers (`write_memory_chunks`, `_insert_session_summary`)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.memory.api.models_evidence_transfer import (
    EvidenceChunk,
    EvidenceDigest,
    EvidenceEdge,
    MemoryExportEvidenceInput,
    MemoryExportEvidenceOutput,
    MemoryImportEvidenceInput,
    MemoryImportEvidenceOutput,
)
from server.memory.domain.okf_evidence import evidence_is_high

log = logging.getLogger(__name__)


def _encode_cursor(session_id: str, chunk_index: int) -> str:
    raw = json.dumps([session_id, int(chunk_index)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int] | None:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        sid, idx = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        return str(sid), int(idx)
    except Exception:  # noqa: BLE001 — a bad cursor restarts the page, not the request
        return None


def _text(v: Any) -> str:
    """FixedString columns (content_sha256) arrive as bytes from the driver."""
    if v is None:
        return ""
    if isinstance(v, bytes | bytearray):
        return bytes(v).decode("utf-8", "replace").rstrip("\x00")
    return str(v)


async def _embed_all(embedder: Any, texts: list[str]) -> list[list[float]]:
    """One batched call where the provider has it (every shipped embedder does;
    the Protocol only promises `embed`), else one call per text."""
    many = getattr(embedder, "embed_many", None)
    if many is not None:
        vectors = await many(texts)
        if len(vectors) != len(texts):
            raise RuntimeError(
                "import_evidence: embed_many returned "
                f"{len(vectors)} vectors for {len(texts)} texts"
            )
        return [list(v) for v in vectors]
    return [list(await embedder.embed(t)) for t in texts]


def _iso(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat()
    return str(v)


def _decode_edge_cursor(cursor: str) -> tuple[str, str, str] | None:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        a, b, c = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        return str(a), str(b), str(c)
    except Exception:  # noqa: BLE001 — a bad cursor restarts the page, not the request
        return None


async def _export_edges(input: MemoryExportEvidenceInput) -> MemoryExportEvidenceOutput:
    """`kind: edges`: the namespace's live typed links, paged by the table key
    `(from_key, to_key, relation)`."""
    from server.memory.store import operator_edges as operator_edges_repo

    rows = await operator_edges_repo.list_live(
        input.org,
        input.namespace,
        limit=int(input.limit) + 1,
        after=_decode_edge_cursor(input.cursor),
    )
    has_more = len(rows) > input.limit
    rows = rows[: input.limit]
    edges = [
        EvidenceEdge(
            from_key=str(r["from_key"]),
            to_key=str(r["to_key"]),
            relation=str(r["relation"]),
            confidence=float(r.get("confidence") or 1.0),
            explanation=str(r.get("explanation") or ""),
            created_at=_iso(r.get("created_at")),
        )
        for r in rows
    ]
    next_cursor = ""
    if has_more and edges:
        raw = json.dumps([edges[-1].from_key, edges[-1].to_key, edges[-1].relation]).encode()
        next_cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return MemoryExportEvidenceOutput(edges=edges, has_more=has_more, next_cursor=next_cursor)


async def handle_memory_export_evidence(
    input: MemoryExportEvidenceInput,
) -> MemoryExportEvidenceOutput:
    if input.kind == "edges":
        return await _export_edges(input)
    ch = get_client()
    params: dict[str, Any] = {"org": input.org, "ns": input.namespace, "lim": int(input.limit) + 1}
    where = ["org = {org:String}", "namespace = {ns:String}", "tombstoned_at IS NULL"]
    if input.session_id:
        where.append("session_id = {sid:String}")
        params["sid"] = input.session_id

    if input.kind == "digests":
        rows = await ch.query_all(
            f"""
            SELECT session_id, agent, user_id, summary_text, key_facts, derived_from,
                   valid_at, created_at
            FROM loom.session_summaries FINAL
            WHERE {" AND ".join(where)}
            ORDER BY session_id ASC, version DESC
            LIMIT 1 BY session_id
            LIMIT {{lim:UInt32}}
            """,
            params=params,
        )
        digests = [
            EvidenceDigest(
                session_id=str(r["session_id"]),
                summary_text=str(r.get("summary_text") or ""),
                agent=str(r.get("agent") or "-"),
                user_id=str(r.get("user_id") or "-"),
                key_facts=[str(k) for k in (r.get("key_facts") or [])],
                derived_from=[str(k) for k in (r.get("derived_from") or [])],
                valid_at=_iso(r.get("valid_at")),
                created_at=_iso(r.get("created_at")),
            )
            for r in rows[: input.limit]
        ]
        kept = await _without_high(
            input, digests, lambda d: (d.derived_from, "\n".join([d.summary_text, *d.key_facts]))
        )
        return MemoryExportEvidenceOutput(
            digests=kept, has_more=len(rows) > input.limit, held_high=len(digests) - len(kept)
        )

    after = _decode_cursor(input.cursor)
    if after is not None:
        where.append("(session_id, chunk_index) > ({c_sid:String}, {c_idx:UInt32})")
        params["c_sid"], params["c_idx"] = after
    rows = await ch.query_all(
        f"""
        SELECT session_id, chunk_index, content_sha256, content, agent, user_id,
               parent_memory_key, line_start, line_end, created_at, source_url
        FROM loom.memory_chunks FINAL
        WHERE {" AND ".join(where)}
        ORDER BY session_id ASC, chunk_index ASC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    has_more = len(rows) > input.limit
    rows = rows[: input.limit]
    from server.memory.app.chunk_links import chunk_memory_keys

    links = await chunk_memory_keys(
        ch,
        org=input.org,
        namespace=input.namespace,
        chunks=[(str(r["session_id"]), int(r["chunk_index"])) for r in rows],
    )
    chunks = [
        EvidenceChunk(
            session_id=str(r["session_id"]),
            chunk_index=int(r["chunk_index"]),
            content=_text(r.get("content")),
            content_sha256=_text(r.get("content_sha256")),
            agent=str(r.get("agent") or ""),
            user_id=str(r.get("user_id") or "-"),
            parent_memory_key=str(r.get("parent_memory_key") or ""),
            memory_keys=links.get((str(r["session_id"]), int(r["chunk_index"])), []),
            line_start=int(r.get("line_start") or 0),
            line_end=int(r.get("line_end") or 0),
            created_at=_iso(r.get("created_at")),
            source_url=str(r.get("source_url") or ""),
        )
        for r in rows
    ]
    next_cursor = _encode_cursor(chunks[-1].session_id, chunks[-1].chunk_index) if has_more else ""
    kept = await _without_high(
        input, chunks, lambda c: ([c.parent_memory_key, *c.memory_keys], c.content)
    )
    return MemoryExportEvidenceOutput(
        chunks=kept, has_more=has_more, next_cursor=next_cursor, held_high=len(chunks) - len(kept)
    )


async def _without_high(
    input: MemoryExportEvidenceInput,
    items: list[Any],
    parts: Callable[[Any], tuple[list[str], str]],
) -> list[Any]:
    """`items` without those `okf_evidence.evidence_is_high` holds home, given the keys
    of this page labelled 'high'. `parts` gives an item's linked keys and its text."""
    if input.include_high or not items:
        return items
    import server.memory.store as memory_repo

    linked = [k for item in items for k in parts(item)[0] if k]
    high = await memory_repo.high_keys(input.org, input.namespace, linked)
    return [item for item in items if not evidence_is_high(*parts(item), high)]


def _importer_user(input: MemoryImportEvidenceInput) -> str | None:
    """The user an import stamps on every chunk, or None to keep each exported
    chunk's own: the request's user when it names one, else the user its token
    is pinned to (read_user_id) — a pinned token never writes another user's
    rows. A request naming a user other than its token's pin is refused, as a
    read under that token is."""
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import resolve_read_filters

    return resolve_read_filters(
        requested="namespace", user_id=input.user_id, session_id=None, auth=current_auth()
    ).user_id


async def _refused(
    input: MemoryImportEvidenceInput, kind: str, session_id: str, texts: list[str]
) -> bool:
    """Refuse (blacklist), as memory.bulk_set: a matching item is dropped, its pattern's hit
    counted and logged, and the rest of the batch is written. Every text the row stores is
    checked, so a pattern cannot ride in through an imported chunk or digest."""
    from server.memory.app.blacklist_guard import find_match, note_hit

    for text in texts:
        hit = await find_match(org=input.org, namespace=input.namespace, value=text)
        if hit is not None:
            await note_hit(hit.pattern_id)
            log.warning(
                "import_evidence dropped blacklisted %s session=%r pattern=%s scope=%s",
                kind,
                session_id,
                hit.pattern_id,
                hit.scope,
            )
            return True
    return False


async def handle_memory_import_evidence(
    input: MemoryImportEvidenceInput,
) -> MemoryImportEvidenceOutput:
    from server.embedding import get_embedder
    from server.memory.store import build_session_summary_row

    embedder = get_embedder()
    ch = get_client()
    now = naive_utc_now()
    out = MemoryImportEvidenceOutput(embedding_model=embedder.model_name)

    chunks = [
        c
        for c in input.chunks
        if c.content.strip()
        and c.session_id
        and not await _refused(input, "chunk", c.session_id, [c.content])
    ]
    out.skipped += len(input.chunks) - len(chunks)
    importer_user = _importer_user(input)
    if chunks:
        vectors = await _embed_all(embedder, [c.content for c in chunks])
        rows: list[dict[str, Any]] = []
        link_specs: list[tuple[str, int, str, int, int, list[str]]] = []
        for c, vec in zip(chunks, vectors, strict=True):
            if not vec:
                out.skipped += 1
                continue
            sha = hashlib.sha256(c.content.encode("utf-8")).hexdigest()
            if c.content_sha256 and c.content_sha256 != sha:
                out.edited.append(f"{c.session_id}:{c.chunk_index}")
            out.embedding_dim = len(vec)
            rows.append(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "agent": input.agent,
                    "user_id": importer_user or c.user_id or "-",
                    "session_id": c.session_id,
                    "chunk_index": int(c.chunk_index),
                    "content_sha256": sha,
                    "content": c.content,
                    "embedding": vec,
                    "embedding_model": embedder.model_name,
                    "embedding_dim": len(vec),
                    "parent_memory_key": c.parent_memory_key
                    or (c.memory_keys[0] if c.memory_keys else ""),
                    "line_start": int(c.line_start or 0),
                    "line_end": int(c.line_end or 0),
                    "source_url": c.source_url or input.source_trace,
                    "created_at": now,
                    "tombstoned_at": None,
                }
            )
            link_specs.append(
                (
                    c.session_id,
                    int(c.chunk_index),
                    sha,
                    int(c.line_start or 0),
                    int(c.line_end or 0),
                    [k for k in c.memory_keys if k],
                )
            )
        if rows:
            await ch.insert(
                "loom.memory_chunks", rows, settings={"async_insert": 0, "wait_for_async_insert": 1}
            )
            out.chunks_written = len(rows)
        from server.memory.app.chunk_links import SOURCES_TABLE, import_link_rows

        link_rows = import_link_rows(
            org=input.org, namespace=input.namespace, agent=input.agent, chunks=link_specs, now=now
        )
        if link_rows:
            await ch.insert(
                SOURCES_TABLE,
                link_rows,
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
            out.links_written = len(link_rows)

    digests = [
        d
        for d in input.digests
        if d.summary_text.strip()
        and d.session_id
        and not await _refused(input, "digest", d.session_id, [d.summary_text, *d.key_facts])
    ]
    out.skipped += len(input.digests) - len(digests)
    if digests:
        vectors = await _embed_all(embedder, [d.summary_text for d in digests])
        drows: list[dict[str, Any]] = []
        for d, vec in zip(digests, vectors, strict=True):
            if not vec:
                out.skipped += 1
                continue
            out.embedding_dim = len(vec)
            drows.append(
                build_session_summary_row(
                    org=input.org,
                    namespace=input.namespace,
                    agent=input.agent,
                    user_id=importer_user or d.user_id or "-",
                    session_id=d.session_id,
                    run_id=str(getattr(input, "run_id", "") or "-"),
                    summary_text=d.summary_text,
                    summary_embedding=vec,
                    embedding_model=embedder.model_name,
                    embedding_dim=len(vec),
                    key_facts=list(d.key_facts or []),
                    derived_from=list(d.derived_from or []),
                    valid_at=parse_iso_dt(d.valid_at) if d.valid_at else None,
                    created_at=now,
                )
            )
        if drows:
            await ch.insert(
                "loom.session_summaries",
                drows,
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
            out.digests_written = len(drows)
    return out
