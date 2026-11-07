"""Audit / curation surfaces for memory.

- history         — full version trail of one memory_key (incl. tombstoned/redacted)
- timeline        — temporal-window query across the identity tuple
- redact          — scrub a historical row, preserving the version chain
- import_curated  — split a curated markdown doc into per-section memories
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from server.core.ch import CHClient, get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value_json as _content_sha256_of_stored
from server.core.naming import named
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import (
    WHOLE_NAMESPACE,
    ReadFilters,
    scope_and,
    scope_where,
)
from server.memory.api.models_bulk import (
    MemoryImportCuratedInput,
    MemoryImportCuratedOutput,
    MemoryImportCuratedRow,
)
from server.memory.api.models_crud import MemoryRedactInput, MemoryRedactOutput, MemorySetInput
from server.memory.api.models_temporal import (
    MemoryBacklinksInput,
    MemoryBacklinksOutput,
    MemoryDigestsInput,
    MemoryDigestsOutput,
    MemoryHistoryInput,
    MemoryHistoryOutput,
    MemoryOutlineInput,
    MemoryOutlineOutput,
    MemoryTimelineInput,
    MemoryTimelineOutput,
    _BacklinkDigest,
    _BacklinkEdge,
    _BacklinkRelated,
    _BacklinkVersion,
    _DigestRow,
    _MemoryHistoryRow,
    _MemoryTimelineEntry,
    _OutlineCommunity,
    _OutlineHub,
    _OutlineSession,
)
from server.memory.app import webhooks
from server.memory.app.provenance import writer_actor
from server.memory.domain.sensitivity import listed_text, value_reads_high
from server.memory.domain.temporal_sql import status_predicate
from server.memory.domain.webhook_events import memory_change
from server.memory.store.history import HistoryTarget
from server.memory.store.rows import MemoryRow
from server.observability import audit as _audit
from server.observability.budget_shape import note_budget_shape

_CURATED_KEY_SLUG = re.compile(r"[^a-z0-9]+")

log = logging.getLogger(__name__)


def _slugify_for_memory_key(heading: str) -> str:
    """Heading → memory_key. Lowercase, non-alnum → _, trim _."""
    s = heading.lower().strip()
    s = _CURATED_KEY_SLUG.sub("_", s).strip("_")
    return s[:80] or "section"


def _split_curated_doc(content: str, heading_level: int) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections at the given heading
    level. Headings BELOW the split level are kept inline as part of
    the body; headings ABOVE prefix the most recent section's heading
    (`Parent / Child`). Pre-heading prologue (anything before the first
    matching heading) lands under heading 'preface' so nothing is lost.
    """
    lines = content.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading = "preface"
    current_body: list[str] = []
    parent_chain: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        # Track higher-level headings (above the split point) so we
        # can prefix them onto the next at-level heading we see.
        if stripped.startswith("#") and " " in stripped:
            hashes = stripped.split(" ", 1)[0]
            if all(c == "#" for c in hashes):
                depth = len(hashes)
                if depth < heading_level:
                    parent_chain = parent_chain[: depth - 1]
                    parent_chain.append(stripped[len(hashes) :].strip())
                    continue
                if depth == heading_level:
                    if current_body or current_heading != "preface":
                        sections.append((current_heading, current_body))
                    raw = stripped[len(hashes) :].strip()
                    current_heading = " / ".join(parent_chain + [raw]) if parent_chain else raw
                    current_body = []
                    continue
        current_body.append(line)

    if current_body or current_heading != "preface":
        sections.append((current_heading, current_body))

    # Drop empty preface
    result: list[tuple[str, str]] = []
    for h, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body and h == "preface":
            continue
        result.append((h, body))
    return result


async def handle_memory_digests(input: MemoryDigestsInput) -> MemoryDigestsOutput:
    """Session digests of a namespace, oldest session first.

    A digest is the one derived row loom writes on every ingest; serving it on the wire lets a
    caller audit its fidelity to the turns it compresses without database access.
    `session_id` "*" or "-" lists all."""
    from server.core.ch import get_client as _get_client

    ch = _get_client()
    where = "org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL"
    params: dict[str, Any] = {"o": input.org, "n": input.namespace, "lim": int(input.limit) + 1}
    if named(input.session_id, wildcard=True):
        where += " AND session_id = {s:String}"
        params["s"] = input.session_id
    where += scope_and(request_scope(), params)
    rows = await ch.query_all(
        "SELECT session_id, summary_text, key_facts, derived_from, valid_at, created_at, "
        "version FROM loom.session_summaries FINAL WHERE "
        + where
        + " ORDER BY created_at, session_id LIMIT {lim:UInt32}",
        params,
    )
    page = rows[: int(input.limit)]
    # Checking every digest's text is CPU work; it runs off the event loop.
    return MemoryDigestsOutput(
        rows=await asyncio.to_thread(_digest_rows, page),
        truncated=len(rows) > len(page),
    )


def _digest_rows(page: list[dict[str, Any]]) -> list[_DigestRow]:
    return [
        _DigestRow(
            session_id=str(r.get("session_id") or ""),
            summary_text=listed_text(r.get("summary_text")),
            # a digest an earlier build wrote may still hold a high shape as a key fact
            key_facts=[str(k) for k in (r.get("key_facts") or []) if not value_reads_high(k)],
            derived_from=[str(k) for k in (r.get("derived_from") or [])],
            valid_at=r.get("valid_at"),
            created_at=r.get("created_at"),
            version=int(r.get("version") or 0),
        )
        for r in page
    ]


def _headline(text: Any, limit: int = 160) -> str:
    """The first sentence or line of a digest, clipped."""
    t = " ".join(listed_text(text).split())
    for sep in (". ", "; ", " — "):
        i = t.find(sep)
        if 0 < i < limit:
            return t[: i + (1 if sep == ". " else 0)].strip()
    return t[:limit].rstrip()


def _day(v: Any) -> str:
    return str(v or "")[:10]


async def handle_memory_outline(input: MemoryOutlineInput) -> MemoryOutlineOutput:
    """A namespace's table of contents from structure alone: a cheap post-write survey.
    Five indexed reads, no model call."""
    from server.core.ch import get_client as _get_client

    ch = _get_client()
    base = {"o": input.org, "n": input.namespace}
    counts = await ch.query_all(
        "SELECT memory_type, count() AS c FROM loom.memory FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL "
        "AND status = 'active' AND memory_key NOT LIKE 'chunk:%' GROUP BY memory_type",
        base,
    )
    rows_by_type = {str(r["memory_type"]): int(r["c"]) for r in counts}
    sessions: list[_OutlineSession] = []
    sessions_total = 0
    if int(input.sessions_limit) > 0:
        digests = await ch.query_all(
            "SELECT session_id, summary_text, length(key_facts) AS kf, length(derived_from) AS df, "
            "coalesce(valid_at, created_at) AS d FROM loom.session_summaries FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL "
            "ORDER BY d DESC, session_id LIMIT {lim:UInt32}",
            {**base, "lim": int(input.sessions_limit) + 1},
        )
        total = await ch.query_all(
            "SELECT count() AS c FROM loom.session_summaries FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            base,
        )
        sessions_total = int((total or [{}])[0].get("c") or 0)
        page = digests[: int(input.sessions_limit)]
        # Checking every digest's text is CPU work; it runs off the event loop.
        headlines = await asyncio.to_thread(
            lambda: [_headline(r.get("summary_text")) for r in page]
        )
        for r, headline in zip(page, headlines, strict=True):
            sessions.append(
                _OutlineSession(
                    session_id=str(r.get("session_id") or ""),
                    date=_day(r.get("d")),
                    headline=headline,
                    key_facts=int(r.get("kf") or 0),
                    rows=int(r.get("df") or 0),
                )
            )
    hubs: list[_OutlineHub] = []
    if int(input.hubs_limit) > 0:
        deg = await ch.query_all(
            "SELECT entity, max(degree) AS degree FROM loom.entity_degree "
            "WHERE org = {o:String} AND namespace = {n:String} "
            "GROUP BY entity ORDER BY degree DESC, entity LIMIT {lim:UInt32}",
            {**base, "lim": int(input.hubs_limit)},
        )
        hubs = [_OutlineHub(entity=str(r["entity"]), degree=int(r.get("degree") or 0)) for r in deg]
    communities: list[_OutlineCommunity] = []
    if int(input.communities_limit) > 0:
        comms = await ch.query_all(
            "SELECT community_id, any(label) AS label, max(size) AS size, "
            "toString(max(computed_at)) AS last_computed FROM loom.entity_communities "
            "WHERE org = {o:String} AND namespace = {n:String} AND level = 0 "
            "AND computed_at = (SELECT max(computed_at) FROM loom.entity_communities "
            "WHERE org = {o:String} AND namespace = {n:String}) "
            "GROUP BY community_id ORDER BY size DESC, community_id LIMIT {lim:UInt32}",
            {**base, "lim": int(input.communities_limit)},
        )
        communities = [
            _OutlineCommunity(
                community_id=int(r["community_id"]),
                label=str(r.get("label") or ""),
                size=int(r.get("size") or 0),
                computed_at=str(r.get("last_computed") or ""),
            )
            for r in comms
        ]
    profile_chars = 0
    if named(input.user_id):
        prof = await ch.query_all(
            "SELECT length(value) AS chars FROM loom.memory FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND user_id = {u:String} "
            "AND memory_type = 'profile' AND tombstoned_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            {**base, "u": input.user_id},
        )
        profile_chars = int((prof or [{}])[0].get("chars") or 0)
    return MemoryOutlineOutput(
        rows_by_type=rows_by_type,
        sessions=sessions,
        sessions_total=sessions_total,
        hubs=hubs,
        communities=communities,
        profile_chars=profile_chars,
    )


async def handle_memory_backlinks(input: MemoryBacklinksInput) -> MemoryBacklinksOutput:
    """What points at one memory: version chain, digests whose lineage names it, typed links
    in and out, evidence count, and the memories sharing its entities. Six indexed reads, no
    model call."""
    from server.core.ch import get_client as _get_client

    ch = _get_client()
    base = {"o": input.org, "n": input.namespace, "k": input.memory_key}
    lim = int(input.limit)
    versions = await ch.query_all(
        "SELECT version, status, toString(created_at) AS created_at FROM loom.memory_versions "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String} "
        "ORDER BY version DESC LIMIT {lim:UInt32}",
        {**base, "lim": lim},
    )
    digests = await ch.query_all(
        "SELECT session_id, toString(coalesce(valid_at, created_at)) AS d "
        "FROM loom.session_summaries FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL "
        "AND has(derived_from, {k:String}) ORDER BY d DESC LIMIT {lim:UInt32}",
        {**base, "lim": lim},
    )
    links_in = await ch.query_all(
        "SELECT from_key AS memory_key, relation, toString(created_at) AS created_at "
        "FROM loom.operator_edges FINAL WHERE org = {o:String} AND namespace = {n:String} "
        "AND to_key = {k:String} AND tombstoned_at IS NULL "
        "ORDER BY created_at DESC LIMIT {lim:UInt32}",
        {**base, "lim": lim},
    )
    links_out = await ch.query_all(
        "SELECT to_key AS memory_key, relation, toString(created_at) AS created_at "
        "FROM loom.operator_edges FINAL WHERE org = {o:String} AND namespace = {n:String} "
        "AND from_key = {k:String} AND tombstoned_at IS NULL "
        "ORDER BY created_at DESC LIMIT {lim:UInt32}",
        {**base, "lim": lim},
    )
    # Both link tables are ReplacingMergeTree(created_at): a tombstone is a newer
    # version of the same key, so only the latest version says whether it is live.
    ev = await ch.query_all(
        "SELECT count() AS c FROM ("
        "  SELECT evidence_id, argMax(tombstoned_at, created_at) AS tomb_latest "
        "  FROM loom.memory_belief_sources "
        "  WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String} "
        "  GROUP BY evidence_id HAVING tomb_latest IS NULL)",
        base,
    )
    related = await ch.query_all(
        "SELECT memory_key, groupUniqArray(4)(ent) AS shared FROM ("
        "  SELECT other.memory_key AS memory_key, other.entity_a AS ent "
        "  FROM loom.entity_edge_memories AS other "
        "  WHERE other.org = {o:String} AND other.namespace = {n:String} "
        "    AND other.memory_key != {k:String} "
        "    AND other.entity_a IN (SELECT entity_a FROM loom.entity_edge_memories "
        "      WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String} "
        "      UNION ALL SELECT entity_b FROM loom.entity_edge_memories "
        "      WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String})"
        ") GROUP BY memory_key ORDER BY length(shared) DESC, memory_key LIMIT {lim:UInt32}",
        {**base, "lim": lim},
    )
    return MemoryBacklinksOutput(
        memory_key=input.memory_key,
        found=bool(versions),
        versions=[
            _BacklinkVersion(
                version=int(r.get("version") or 0),
                status=str(r.get("status") or ""),
                created_at=str(r.get("created_at") or ""),
            )
            for r in versions
        ],
        digests=[
            _BacklinkDigest(session_id=str(r.get("session_id") or ""), date=_day(r.get("d")))
            for r in digests
        ],
        links_in=[
            _BacklinkEdge(
                memory_key=str(r.get("memory_key") or ""),
                relation=str(r.get("relation") or ""),
                created_at=str(r.get("created_at") or ""),
            )
            for r in links_in
        ],
        links_out=[
            _BacklinkEdge(
                memory_key=str(r.get("memory_key") or ""),
                relation=str(r.get("relation") or ""),
                created_at=str(r.get("created_at") or ""),
            )
            for r in links_out
        ],
        evidence_count=int((ev or [{}])[0].get("c") or 0),
        related=[
            _BacklinkRelated(
                memory_key=str(r.get("memory_key") or ""),
                shared_entities=[str(e) for e in (r.get("shared") or [])],
            )
            for r in related
        ],
    )


async def handle_memory_history(input: MemoryHistoryInput) -> MemoryHistoryOutput:
    """Version history of one memory_key, newest-first, including
    tombstoned/redacted rows — the audit trail. Read from the append-only mirror
    loom.memory_versions; the RMT rows in loom.memory are not a history — a merge keeps only
    the head.
    """
    import server.memory.store as memory_repo

    rows = await memory_repo.list_history(
        memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
        ),
        input.memory_key,
        limit=input.limit,
        scope=request_scope(),
    )
    out_rows = [
        _MemoryHistoryRow(
            memory_id=r.memory_id,
            version=r.version,
            value=memory_repo.parse_memory_value(r.value),
            confidence=float(r.confidence),
            source_trace=r.source_trace,
            pinned=bool(r.pinned),
            created_at=r.created_at,
            expires_at=r.expires_at,
            tombstoned_at=r.tombstoned_at,
            redacted_at=r.redacted_at,
            redaction_reason=r.redaction_reason,
            content_sha256=_content_sha256_of_stored(r.value) if r.value else "",
            actor=r.actor,
            source_class=r.source_class,
            agent=r.agent,
            written_at=_version_time(r.version),
            status=r.status,
            conflict_with=list(r.conflict_with),
            tags=list(r.tags),
            verified_at=r.verified_at,
            verified_by=r.verified_by,
            verified_sha256=r.verified_sha256,
            lock_mode=r.lock_mode,
            lock_reason=r.lock_reason,
            lock_until=r.lock_until,
        )
        for r in rows
    ]
    # Every served version of a row with any 'high' version is audited, as memory.get audits
    # the head: the version before a sweep labelled the row holds the same value unlabelled.
    high_ids = {str(r.memory_id) for r in rows if r.sensitivity == "high"}
    plain_ids = {str(r.memory_id) for r in rows} - high_ids
    high_ids |= await memory_repo.high_memory_ids(
        input.org, input.namespace, input.memory_key, sorted(plain_ids)
    )
    audited = [
        {"memory_key": input.memory_key, "memory_id": str(r.memory_id), "version": r.version}
        for r in rows
        # a redacted version served no value
        if str(r.memory_id) in high_ids and r.redacted_at is None
    ]
    if audited:
        note_budget_shape("audited")
    await _audit.emit_many(
        "memory.sensitive_read",
        org=input.org,
        namespace=input.namespace,
        actor=writer_actor(input.agent),
        subjects=audited,
    )
    return MemoryHistoryOutput(rows=out_rows)


async def fetch_timeline(
    *,
    org: str,
    namespace: str,
    entity: str = "",
    tags: list[str] | None = None,
    memory_key_prefix: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    top_k: int = 50,
    order: str = "desc",
    scope: ReadFilters,
    ch: CHClient | None = None,
) -> list[dict[str, Any]]:
    """CH query behind `memory.timeline`. Returns raw rows ordered by
    `coalesce(valid_at, created_at)` (valid-time only); `since`/`until` filter on
    the same coalesce. Serves what every other read serves: deleted, expired and
    retired (superseded, duplicate, proposed, rejected) rows are left out.

    No scope check here — the empty-filter refusal lives in the wire handler;
    an all-empty scope yields an unscoped query the caller must not make.
    """
    ch = ch or get_client()
    where_clauses = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "embedding_dim > 0",  # embed-pending placeholders excluded
        "scope = 'private'",
        # The latest version of a deleted row keeps its vector, so tombstoned_at is
        # the only thing that says it is gone.
        "tombstoned_at IS NULL",
        "(expires_at IS NULL OR expires_at > now())",
        status_predicate(),
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
    }
    where_clauses.extend(scope_where(scope, params))
    if entity:
        # the canonical form rows are stored in, and the lowercase one older rows kept
        from server.memory.domain.graph_quality import canonical_entity_text

        where_clauses.append("hasAny(entities, {entity:Array(String)})")
        params["entity"] = sorted({canonical_entity_text(entity), entity.lower()} - {""})
    if tags:
        where_clauses.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = list(tags)
    if memory_key_prefix:
        where_clauses.append("startsWith(memory_key, {prefix:String})")
        params["prefix"] = memory_key_prefix
    if since is not None:
        where_clauses.append("coalesce(valid_at, created_at) >= {since:DateTime}")
        params["since"] = since
    if until is not None:
        where_clauses.append("coalesce(valid_at, created_at) <= {until:DateTime}")
        params["until"] = until

    order_dir = "DESC" if order == "desc" else "ASC"
    params["lim"] = int(top_k)

    # count() OVER () returns the total matching alongside the top_k page,
    # so count + page is one round-trip.
    sql = f"""
    SELECT
        memory_key, memory_type, content_excerpt,
        valid_at, created_at, namespace,
        count() OVER () AS total_matching
    FROM loom.memory_embeddings FINAL
    WHERE {" AND ".join(where_clauses)}
    -- created_at is second-precision; tie-break by version (nanosecond write
    -- order) so same-second events still order by when they happened, not by
    -- memory_key alphabetically.
    ORDER BY coalesce(valid_at, created_at) {order_dir}, version {order_dir}
    LIMIT {{lim:UInt32}}
    """
    return await ch.query_all(sql, params=params)


async def handle_memory_timeline(input: MemoryTimelineInput) -> MemoryTimelineOutput:
    """Memories about an entity (or tag/key-prefix scope) in temporal
    order — answers "what happened with X over time", which vector search
    structurally can't (cosine has no ordering). Query lives in
    `fetch_timeline`.
    """
    if not (input.entity or input.tags or input.memory_key_prefix):
        # Refuse a full-table scan. Caller must scope the query.
        raise HTTPException(
            status_code=400,
            detail={
                "code": "TIMELINE_REQUIRES_FILTER",
                "message": "Provide at least one of entity / tags / memory_key_prefix",
            },
        )

    rows = await fetch_timeline(
        org=input.org,
        namespace=input.namespace,
        entity=input.entity,
        tags=list(input.tags),
        memory_key_prefix=input.memory_key_prefix,
        since=input.since,
        until=input.until,
        top_k=int(input.top_k),
        order=input.order,
        scope=request_scope(),
    )

    events = [
        _MemoryTimelineEntry(
            memory_key=str(r["memory_key"]),
            memory_type=str(r["memory_type"]),
            content_excerpt=str(r["content_excerpt"]),
            valid_at=r.get("valid_at"),
            created_at=r["created_at"],
            namespace=str(r["namespace"]),
        )
        for r in rows
    ]
    total = int(rows[0]["total_matching"]) if rows else 0
    return MemoryTimelineOutput(events=events, total_matching=total)


async def handle_memory_redact(input: MemoryRedactInput) -> MemoryRedactOutput:
    """Scrub the text of a historical memory version in place, and every
    derived copy of it (see `memory_repo.redaction` for the columns).

    Refuses the live head — the caller must memory.delete first, so this
    audit-time op can't silently lose live data. A lock on the memory's key, or
    on a profile key built from it, refuses the whole redact.
    """
    import server.memory.store as memory_repo

    version = int(input.version)  # the validator took a decimal string to an int
    target = await memory_repo.find_history_target(input.org, input.memory_id, version)
    if target is None:
        return MemoryRedactOutput(redacted=False)
    # Addressed by org-wide id: ADMIN on the row's namespace is checked here
    # (the transport had no namespace to bind).
    from server.auth.authz import row_in_scope
    from server.operations.base import Role as _Role

    if not await row_in_scope(input.org, target.namespace, _Role.ADMIN):
        return MemoryRedactOutput(redacted=False)

    # All versions of a memory share one memory_id, so "is this the live head"
    # is a VERSION comparison, not a memory_id one. Refuse only when the caller
    # targets the head's version — scrubbing that would destroy live data.
    identity = memory_repo.Identity(
        org=input.org,
        namespace=target.namespace,
        agent=target.agent,
        user_id=target.user_id,
        session_id=target.session_id,
    )
    # A hashed search row stamped after this is a newer write's, unless it carries the
    # redacted text.
    through = memory_repo.next_version()
    head = await memory_repo.get_by_key(
        identity,
        target.memory_key,
        fresh=True,  # the head must be seen: a miss here could let a redact hit live data
        strong=True,  # ... and so must its latest version
        scope=WHOLE_NAMESPACE,
    )
    if head is not None and head.version == version:
        return MemoryRedactOutput(redacted=False, refused_head=True)
    if head is not None:
        # Scrubbing a historical version of a locked key is a deletion of its
        # evidence: both lock modes refuse it.
        from server.memory.domain import lock as _lock

        _lock.assert_unlocked(head, "delete", op="memory.redact")
    # A lock on a profile key built from this memory refuses the redact before
    # anything is scrubbed, as a lock on the memory's own key does.
    profiles = await _profiles_built_from(input.org, target, input.memory_id)

    now = naive_utc_now()
    reason_blob = (input.reason or "")[:512]
    by_blob = (input.redacted_by or "")[:128]
    # The named version, its copies on the key and every profile version built from it go
    # in one call: two mutations. A failure after them is reported, and a retry reaches
    # the same rows, since the lookups count redacted versions too.
    copies = await memory_repo.redact_versions(
        (
            input.org,
            target.namespace,
            target.agent,
            target.user_id,
            target.session_id,
            target.memory_key,
        ),
        input.memory_id,
        version,
        when=now,
        reason_blob=f"{reason_blob}|{by_blob}",
        head=(str(head.memory_id), head.version) if head is not None else None,
    )
    # The key's embedding and chunk rows are per key, not per version: scrub every one
    # whose text is not the live head's, the head read again now (every older one once
    # the key is deleted or its head was scrubbed).
    head_now = await memory_repo.get_by_key(
        identity, target.memory_key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
    )
    if head_now is not None and head_now.redacted_at is not None:
        head_now = None
    scrubs = await memory_repo.key_derivative_scrubs(
        input.org,
        target.namespace,
        target.agent,
        target.user_id,
        target.session_id,
        target.memory_key,
        head_live=head_now is not None,
        live_link_inputs=await _live_link_inputs(input.org, head_now),
        through=through,
        redacted_sha=target.content_sha256,
        live_sha=_live_sha(head_now),
        set_chunks_kept=await _kept_set_chunks(
            input.org, target.namespace, target.agent, target.user_id, target.memory_key
        ),
    )
    await memory_repo.run_scrubs(scrubs, apply=True)
    await _scrub_profile_derivatives(input.org, target, profiles, through=through)
    # A redacted value may already be quoted in a cached answer (7-day TTL), a precedent
    # (30 days) or a cached code.ask answer; each carries the keys of the memory it was
    # shown, so the text leaves with the row it quoted. A failure here is reported, not
    # swallowed: an ADMIN scrub that left the quote in place is not a completed redaction.
    # A scrubbed profile key may be the only row of a window that quoted the text. Windows
    # hold bare keys and every user's profile shares its key, so this reach is org-wide.
    cache_rows = precedent_rows = 0
    for key in [target.memory_key, *sorted({p.memory_key for p in profiles})]:
        cache_rows += await memory_repo.delete_llm_response_cache_by_key(input.org, key)
        cache_rows += await memory_repo.delete_code_answers_by_key(input.org, key)
        precedent_rows += await memory_repo.delete_ask_precedents_by_key(input.org, key)
    # Receivers hear of each version scrubbed, the later copies on the key included,
    # and of the newest one of each profile key built from the memory.
    await webhooks.announce(
        [("memory.redacted", memory_change(target, v)) for v in [version, *copies]]
        + [("memory.redacted", memory_change(p.head, p.newest)) for p in profiles if p.head]
    )
    return MemoryRedactOutput(
        redacted=True,
        redacted_at=now,
        cache_rows_deleted=cache_rows,
        precedent_rows_deleted=precedent_rows,
    )


async def quotes_a_redacted_key(org: str, namespace: str, keys: list[str]) -> bool:
    """For code.ask: whether any of `keys` of `namespace` has a redacted version. An
    unreadable stamp counts as one."""
    import server.memory.store as memory_repo

    try:
        return await memory_repo.keys_redacted(org, namespace, keys)
    except Exception as e:  # noqa: BLE001 — unknown: the answer is not cached
        log.debug("code.ask redaction read failed: %s", e)
        return True


async def delete_code_answers_quoting(org: str, keys: list[str]) -> None:
    """Every cached code.ask answer whose memory excerpts held any of `keys`."""
    import server.memory.store as memory_repo

    await memory_repo.delete_code_answers_quoting(org, keys)


@dataclass
class _ProfileScrub:
    """One profile key built from a redacted memory: the newest version built from
    it and the key's head."""

    agent: str
    session_id: str
    memory_key: str
    newest: int
    head: MemoryRow | None = None


async def _profiles_built_from(
    org: str, target: HistoryTarget, memory_id: str
) -> list[_ProfileScrub]:
    """The user's profile keys with a version built from the memory, each with its
    head; raises 423 LOCKED when a key's lock refuses a delete."""
    import server.memory.store as memory_repo
    from server.memory.domain import lock as _lock

    rows = await memory_repo.profile_keys_built_from(
        org, target.namespace, target.user_id, memory_id
    )
    profiles = [
        _ProfileScrub(str(r["agent"]), str(r["session_id"]), str(r["memory_key"]), int(r["newest"]))
        for r in rows
    ]
    for p in profiles:
        p.head = await memory_repo.get_by_key(
            memory_repo.Identity(
                org=org,
                namespace=target.namespace,
                agent=p.agent,
                user_id=target.user_id,
                session_id=p.session_id,
            ),
            p.memory_key,
            fresh=True,
            strong=True,
            scope=WHOLE_NAMESPACE,
        )
        _lock.assert_unlocked(p.head, "delete", op="memory.redact")
    return profiles


async def _scrub_profile_derivatives(
    org: str, target: HistoryTarget, profiles: list[_ProfileScrub], *, through: int
) -> None:
    """Scrub each listed profile key's derived rows: a model wrote them from the
    redacted text. The caller has scrubbed the versions; the worker rebuilds a
    scrubbed head from the user's current facts."""
    import server.memory.store as memory_repo

    for p in profiles:
        # The head read again now: one folded since without the memory keeps its rows. A
        # scrubbed head keeps no text, so none of the key's derived rows is live then.
        head = await memory_repo.get_by_key(
            memory_repo.Identity(
                org=org,
                namespace=target.namespace,
                agent=p.agent,
                user_id=target.user_id,
                session_id=p.session_id,
            ),
            p.memory_key,
            fresh=True,
            strong=True,
            scope=WHOLE_NAMESPACE,
        )
        if head is not None and (head.version <= p.newest or head.redacted_at is not None):
            head = None
        scrubs = await memory_repo.key_derivative_scrubs(
            org,
            target.namespace,
            p.agent,
            target.user_id,
            p.session_id,
            p.memory_key,
            head_live=head is not None,
            live_link_inputs=await _live_link_inputs(org, head),
            # A dropped head bounds nothing: a fold racing the redaction loses its search
            # row too, and the next fold rebuilds it.
            through=through if head is not None else None,
            live_sha=_live_sha(head),
            set_chunks_kept=await _kept_set_chunks(
                org, target.namespace, p.agent, target.user_id, p.memory_key
            ),
        )
        await memory_repo.run_scrubs(scrubs, apply=True)


def _live_sha(head: MemoryRow | None) -> str:
    return head.content_sha256 if head is not None else ""


async def _kept_set_chunks(
    org: str, namespace: str, agent: str, user_id: str, key: str
) -> list[str] | None:
    """Under Postgres, the chunk hashes the key's live heads write, in any session; None
    on the ClickHouse authority, whose live chunks are the heads'."""
    import server.memory.store as memory_repo
    from server.core import pg
    from server.memory.app.set_chunks import set_chunk_identities
    from server.memory.store import pg_memory

    if not pg.enabled():
        return None
    ident = (namespace, agent, user_id, key)
    heads = (await pg_memory.chunk_heads(org, [ident], servable=False)).get(ident, [])
    if not heads:
        return []
    reach = await memory_repo.set_chunk_count(org, namespace, agent, user_id, key)
    return sorted(
        {
            sha
            for value, kind in heads
            for _, sha in set_chunk_identities(value, kind, key, at_least=reach)
        }
    )


async def _live_link_inputs(org: str, head: MemoryRow | None) -> str:
    """The inputs hash a code link made from the live head carries ('' with none)."""
    from server.memory.app.code_link_reads import inputs_now
    from server.memory.store import code_links

    # in the head shape the link worker hashes, so the two can only agree
    rows = await code_links.memory_heads(org, [head.memory_id]) if head is not None else []
    return inputs_now(org, rows[0]) if rows else ""


async def handle_memory_import_curated(
    input: MemoryImportCuratedInput,
) -> MemoryImportCuratedOutput:
    """Split a curated markdown doc into sections, write each via
    handle_memory_set, returning per-section verdicts.

    The caller chose to import this doc, so we don't queue candidates for
    review — but check_on_commit still fires, so a section contradicting a
    pinned memory is rejected per-row.
    """
    from server.memory.app.write import handle_memory_set

    sections = _split_curated_doc(input.content, input.heading_level)
    rows: list[MemoryImportCuratedRow] = []
    written = merged = rejected = skipped = 0

    source_uri = (input.source_uri or "").strip() or "curated_doc"
    base_tags = ["source-kind:curated_doc"] + list(input.extra_tags)

    for heading, body in sections:
        if not body.strip():
            rows.append(
                MemoryImportCuratedRow(
                    memory_key=_slugify_for_memory_key(heading),
                    section_heading=heading,
                    bytes_written=0,
                    verdict="skipped",
                    rejection_reason="empty section body",
                )
            )
            skipped += 1
            continue

        memory_key = _slugify_for_memory_key(heading)
        set_input = MemorySetInput(
            org=input.org,
            namespace=input.namespace,
            agent="-",
            user_id="-",
            session_id="-",
            memory_key=memory_key,
            memory_type="semantic",
            value=body,
            tags=base_tags + [f"section:{heading[:80]}"],
            confidence=1.0,
            source_trace=f"curated_doc://{source_uri}#{heading}",
        )

        try:
            result = await handle_memory_set(set_input)
        except HTTPException as e:
            detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
            code = (detail or {}).get("code") if isinstance(detail, dict) else None
            if code == "PINNED_CONFLICT":
                rows.append(
                    MemoryImportCuratedRow(
                        memory_key=memory_key,
                        section_heading=heading,
                        bytes_written=0,
                        verdict="rejected_pinned",
                        rejection_reason=(
                            (detail or {}).get("message", "") if isinstance(detail, dict) else ""
                        ),
                        pinned_blocker_id=(
                            (detail or {}).get("pinned_blocker_id")
                            if isinstance(detail, dict)
                            else None
                        ),
                    )
                )
                rejected += 1
                continue
            # Any other failure: report skipped rather than abort the import.
            rows.append(
                MemoryImportCuratedRow(
                    memory_key=memory_key,
                    section_heading=heading,
                    bytes_written=0,
                    verdict="skipped",
                    rejection_reason=f"{code or 'ERROR'}: {(detail or {}).get('message', '')[:200] if isinstance(detail, dict) else str(detail)[:200]}",  # noqa: E501
                )
            )
            skipped += 1
            continue

        # duplicate_of set means the write deduped onto an existing memory;
        # there's no separate "merged" signal, so dedup counts as merged.
        if result.duplicate_of:
            verdict = "duplicate"
            merged += 1
        else:
            verdict = "written"
            written += 1
        rows.append(
            MemoryImportCuratedRow(
                memory_key=memory_key,
                section_heading=heading,
                bytes_written=len(body),
                verdict=verdict,
            )
        )

    return MemoryImportCuratedOutput(
        rows=rows,
        written=written,
        merged=merged,
        rejected=rejected,
        skipped=skipped,
    )


def _version_time(version: Any) -> Any:
    """A version's write time: versions are epoch nanoseconds (`time.time_ns()`); anything
    outside a sane range is None."""
    from server.memory.store.rows import version_time

    try:
        ns = int(version)
    except (TypeError, ValueError):
        return None
    if not 10**18 <= ns < 4 * 10**18:  # 2001..2096
        return None
    return version_time(ns)
